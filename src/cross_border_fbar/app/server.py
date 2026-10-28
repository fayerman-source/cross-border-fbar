"""Local review app: the worksheet, its sources, and the CPA's sign-off. Standard library only.

    python -m cross_border_fbar.app --set out/synth --readings results/muse-spark-1.3_synth.json

Binds to 127.0.0.1. Page images are served only from the set's pages/ folder.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
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


def make_handler(review: Review, set_dir: Path):
    pages_dir = (set_dir / "pages").resolve()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # quiet console
            pass

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, status: int = 200) -> None:
            self._send(
                status, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8"
            )

        def _file(self, path: Path) -> None:
            ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            self._send(200, path.read_bytes(), ctype)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                return self._file(STATIC / "index.html")
            if path.startswith("/static/"):
                target = (STATIC / path.removeprefix("/static/")).resolve()
                if target.is_file() and STATIC.resolve() in target.parents:
                    return self._file(target)
            if path.startswith("/fonts/"):  # the repo's Korean font, for the client summary
                target = (FONTS_DIR / path.removeprefix("/fonts/")).resolve()
                if target.is_file() and target.parent == FONTS_DIR:
                    return self._file(target)
            if path.startswith("/doc/pages/"):
                target = (pages_dir / path.removeprefix("/doc/pages/")).resolve()
                if target.is_file() and target.parent == pages_dir:
                    return self._file(target)
            if path == "/api/state":
                return self._json(review.view())
            if path == "/api/export":
                try:
                    return self._json(review.export())
                except ReviewError as e:
                    return self._json({"error": str(e)}, HTTPStatus.CONFLICT)
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

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
