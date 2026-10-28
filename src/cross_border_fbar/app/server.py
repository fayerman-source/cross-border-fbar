"""Local review app: the worksheet, its sources, and the CPA's sign-off. Standard library only.

    python -m cross_border_fbar.app --set out/synth --readings results/muse-spark-1.3_synth.json

Binds to 127.0.0.1. Page images are served only from the set's pages/ folder.
"""

from __future__ import annotations

import argparse
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..llm import Client
from .review import Review, ReviewError
from .worksheet import Inputs

STATIC = Path(__file__).parent / "static"
FONTS_DIR = (Path(__file__).resolve().parents[3] / "fonts").resolve()
PHRASES_PROMPT = (Path(__file__).parent / "prompts" / "client_phrases_v2.md").read_text()
SUMMARY_MODEL = "muse-spark-1.3"


def translate_phrases(phrases: dict, language: str, fresh: bool = False) -> tuple[dict | None, dict]:
    """Translate the summary's fixed phrases. The first draft may replay a recorded response (offline
    demo); a redraft always asks the model. Returns None for a response that isn't the JSON asked for."""
    client = Client(mode="live" if fresh else "auto")
    prompt = PHRASES_PROMPT.replace("{language}", language) + json.dumps(
        phrases, ensure_ascii=False, indent=1
    )
    schema = {
        "type": "object",
        "properties": {k: {"type": "string"} for k in phrases},
        "required": list(phrases),
        "additionalProperties": False,
    }
    fmt = {"type": "json_schema", "json_schema": {"name": "Phrases", "schema": schema}}
    r = client.chat(
        SUMMARY_MODEL,
        [{"role": "user", "content": prompt}],
        max_tokens=6000,
        response_format=fmt,
        label="client-phrases",
    )
    try:
        translations = json.loads(r.content)
    except json.JSONDecodeError:
        translations = None
    meta = {
        "model": SUMMARY_MODEL,
        "prompt": "client_phrases_v2",
        "cost_usd": r.cost_usd,
        "replayed": r.replayed,
    }
    return translations, meta


# Only these file types are ever served, each with a fixed content type.
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".png": "image/png",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".txt": "text/plain; charset=utf-8",
}
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'self'; img-src 'self'; style-src 'self'; script-src 'self'; "
    "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def asset_index(set_dir: Path) -> dict[str, Path]:
    """Every file the app may serve, by URL, built once at startup. Requests only look paths up here;
    nothing in a request is ever turned into a filesystem path."""
    index = {"/": STATIC / "index.html", "/index.html": STATIC / "index.html"}
    roots = {"/static/": STATIC, "/fonts/": FONTS_DIR, "/doc/pages/": set_dir / "pages"}
    for prefix, root in roots.items():
        for f in sorted(root.rglob("*") if prefix == "/static/" else root.glob("*")):
            if f.is_file() and f.suffix in CONTENT_TYPES:
                index[prefix + f.relative_to(root).as_posix()] = f.resolve()
    return index


class Handler(BaseHTTPRequestHandler):
    """Bound to a review and its asset index by make_handler()."""

    review: Review
    assets: dict[str, Path]

    def log_message(self, fmt, *args):  # quiet console
        pass

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self._send(status, body, "application/json; charset=utf-8")

    def _api_get(self, path: str):
        if path == "/api/state":
            return self.review.view()
        if path == "/api/export":
            return self.review.export()
        return None

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        asset = self.assets.get(path)
        if asset is not None:
            return self._send(200, asset.read_bytes(), CONTENT_TYPES[asset.suffix])
        try:
            payload = self._api_get(path)
        except ReviewError as e:
            return self._json({"error": str(e)}, HTTPStatus.CONFLICT)
        if payload is None:
            return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        self._json(payload)

    def _post_action(self, body: dict):
        review, who = self.review, str(body.get("reviewer", "")).strip() or "reviewer"
        actions = {
            "/api/ack": lambda: review.acknowledge(body["flag"], str(body.get("note", "")), who),
            "/api/reopen": lambda: review.reopen(body["flag"], who),
            "/api/ownership": lambda: review.set_ownership(
                body["account"], body["ownership"], str(body.get("note", "")), who
            ),
            "/api/signoff": lambda: review.sign_off(who, str(body.get("initials", ""))),
            "/api/summary/draft": lambda: review.draft_summary(translate_phrases),
            "/api/summary/approve": lambda: review.approve_summary(
                who, body.get("confirm_unreviewed") is True
            ),
        }
        return actions.get(self.path)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._json({"error": "invalid JSON"}, HTTPStatus.BAD_REQUEST)
        action = self._post_action(body)
        if action is None:
            return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        try:
            action()
        except ReviewError as e:
            return self._json({"error": str(e)}, HTTPStatus.CONFLICT)
        except KeyError as e:
            return self._json({"error": f"missing field {e}"}, HTTPStatus.BAD_REQUEST)
        except RuntimeError as e:  # model call failed
            return self._json({"error": f"The summary couldn't be drafted: {e}"}, HTTPStatus.BAD_GATEWAY)
        self._json(self.review.view())


def make_handler(review: Review, set_dir: Path) -> type[Handler]:
    return type("BoundHandler", (Handler,), {"review": review, "assets": asset_index(set_dir)})


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--set", type=Path, default=Path("out/synth"))
    p.add_argument("--readings", type=Path, default=Path("results/muse-spark-1.3_synth.json"))
    p.add_argument("--intake", type=Path)
    p.add_argument("--review-dir", type=Path, default=Path("out/review"))
    p.add_argument("--port", type=int, default=8765)
    a = p.parse_args()
    inputs = Inputs(a.set, a.readings, a.intake or a.set / "intake.json")
    review = Review(inputs, a.review_dir)
    server = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(review, a.set))
    print(f"FBAR review: http://127.0.0.1:{a.port}  (review state in {a.review_dir})")
    server.serve_forever()
