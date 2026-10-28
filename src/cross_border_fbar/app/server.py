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
SUMMARY_PROMPT = (Path(__file__).parent / "prompts" / "client_summary_ko_v1.md").read_text()
SUMMARY_MODEL = "muse-spark-1.3"


def generate_summary(facts: dict) -> tuple[str, dict]:
    client = Client(mode="auto")
    prompt = SUMMARY_PROMPT + json.dumps(facts, ensure_ascii=False, indent=1)
    r = client.chat(
        SUMMARY_MODEL, [{"role": "user", "content": prompt}], max_tokens=6000, label="client-summary"
    )
    return r.content.strip(), {
        "model": SUMMARY_MODEL,
        "prompt": "client_summary_ko_v1",
        "cost_usd": r.cost_usd,
        "replayed": r.replayed,
    }


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


def contained(root: Path, relative: str, flat: bool = False) -> Path | None:
    """The file under root that relative names, or None if it escapes root or isn't a servable type."""
    root = root.resolve()
    target = (root / relative).resolve()
    inside = target.parent == root if flat else root in target.parents
    if inside and target.is_file() and target.suffix in CONTENT_TYPES:
        return target
    return None


def make_handler(review: Review, set_dir: Path):
    # URL prefix -> (folder, flat): flat folders serve direct children only.
    file_roots = {
        "/static/": (STATIC, False),
        "/fonts/": (FONTS_DIR, True),  # the repo's Korean font, for the client summary
        "/doc/pages/": (set_dir / "pages", True),
    }

    class Handler(BaseHTTPRequestHandler):
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

        def _file(self, path: Path) -> None:
            self._send(200, path.read_bytes(), CONTENT_TYPES[path.suffix])

        def _static_target(self, path: str) -> Path | None:
            if path in ("/", "/index.html"):
                return STATIC / "index.html"
            for prefix, (root, flat) in file_roots.items():
                if path.startswith(prefix):
                    return contained(root, path.removeprefix(prefix), flat)
            return None

        def _api_get(self, path: str):
            if path == "/api/state":
                return review.view()
            if path == "/api/export":
                return review.export()
            return None

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            target = self._static_target(path)
            if target is not None:
                return self._file(target)
            try:
                payload = self._api_get(path)
            except ReviewError as e:
                return self._json({"error": str(e)}, HTTPStatus.CONFLICT)
            if payload is None:
                return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            self._json(payload)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "invalid JSON"}, HTTPStatus.BAD_REQUEST)
            who = str(body.get("reviewer", "")).strip() or "reviewer"
            actions = {
                "/api/ack": lambda: review.acknowledge(body["flag"], str(body.get("note", "")), who),
                "/api/reopen": lambda: review.reopen(body["flag"], who),
                "/api/ownership": lambda: review.set_ownership(
                    body["account"], body["ownership"], str(body.get("note", "")), who
                ),
                "/api/signoff": lambda: review.sign_off(who, str(body.get("initials", ""))),
                "/api/summary/draft": lambda: review.draft_summary(generate_summary),
                "/api/summary/approve": lambda: review.approve_summary(who),
            }
            action = actions.get(self.path)
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
            self._json(review.view())

    return Handler


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
