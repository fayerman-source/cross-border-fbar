"""The public demo: the same review app, one private temporary worksheet per visitor, no model calls.

    python -m cross_border_fbar.app --hosted --public-host demo.example.org

Runs behind a proxy that terminates HTTPS and passes the browser's Host header through. Forwarded headers
are never read: the Host must be exactly the configured public host, and a POST's Origin must be exactly
https:// plus that host. The visitor cookie is always Secure, so over plain HTTP a browser won't keep it.

Isolation comes from each visitor having a separate Review and folder, not from anything in the page. The
cookie is a random token looked up in memory; it never names a file. The inputs are only read, and every
request for a visitor holds that visitor's lock, including the eviction that deletes their folder.

The page refuses framing, so the demo is linked by its own URL. It is not meant to work inside another
site's iframe, and the cookie is SameSite=Strict accordingly.
"""

from __future__ import annotations

import contextlib
import json
import re
import secrets
import shutil
import sys
import tempfile
import threading
import time
from collections import OrderedDict
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from pathlib import Path

from . import phrases as phrase_tables
from .phrases import LANGUAGES
from .review import Review
from .server import NOT_THIS_APP, Handler, asset_index
from .worksheet import Inputs

DEFAULT_PORT = 7860
HOST_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
PUBLIC_HOST = re.compile(rf"{HOST_LABEL}(?:\.{HOST_LABEL})*(?::[0-9]{{1,5}})?")
COOKIE = "__Host-fbar_visitor"  # the prefix makes browsers require Secure, Path=/ and no Domain
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")  # secrets.token_urlsafe(32)
VISITOR_GETS = frozenset({"/api/state", "/api/export"})
RESET_PATH = "/api/demo/reset"

SESSION_ENDED = (
    "Your demo session ended, so this is a fresh worksheet and earlier changes are gone. "
    "If this keeps happening, allow cookies for this site."
)
FULL = "This demo worksheet has reached its size limit. Open a new private window to start a fresh one."


@dataclass(frozen=True)
class Limits:
    max_visitors: int = 100
    idle_seconds: float = 2 * 60 * 60
    max_body_bytes: int = 16 * 1024
    max_note_chars: int = 2000
    max_field_chars: int = 200
    max_visitor_disk_bytes: int = 512 * 1024
    max_connections: int = 64
    socket_timeout_seconds: float = 30


def public_host_from(flag: str | None, env: Mapping[str, str]) -> str:
    """The configured public host, or ValueError. There is deliberately no default to fall back on."""
    host = flag or env.get("FBAR_PUBLIC_HOST") or env.get("SPACE_HOST")
    if not host:
        raise ValueError("--hosted needs its public host: pass --public-host or set FBAR_PUBLIC_HOST")
    host = host.strip().lower()
    if len(host) > 253 or not PUBLIC_HOST.fullmatch(host):
        raise ValueError(f"public host {host!r} must be a bare host name, with no scheme or path")
    return host


def require_reviewed_catalog() -> None:
    """Visitors only ever see Korean a person reviewed, so an unreviewed phrase stops the demo starting."""
    phrases = phrase_tables.current_phrases()
    for language in LANGUAGES:
        unreviewed = sorted(set(phrases) - set(phrase_tables.reviewed(language, phrases)))
        if unreviewed:
            raise SystemExit(f"the {language} catalog has phrases not yet reviewed: {unreviewed}")


@dataclass(eq=False)
class Visitor:
    review: Review
    last_seen: float
    lock: threading.Lock = field(default_factory=threading.Lock)
    retired: bool = False

    def disk_bytes(self) -> int:
        paths = (self.review.state_path, self.review.audit_path)
        return sum(path.stat().st_size for path in paths if path.exists())

    def retire(self) -> None:
        """Waits for a request in progress, so its folder can't vanish under it."""
        with self.lock:
            self.retired = True
            shutil.rmtree(self.review.dir, ignore_errors=True)


class VisitorStore:
    """Each visitor's worksheet, least recently used first. Holds at most limits.max_visitors, and removes
    those idle past limits.idle_seconds as new visitors arrive or when they return."""

    def __init__(self, inputs: Inputs, limits: Limits | None = None, clock=time.monotonic):
        self.inputs = inputs if inputs.snap_cache is not None else replace(inputs, snap_cache={})
        self.limits = limits or Limits()
        self._clock = clock
        self._lock = threading.Lock()
        self._visitors: OrderedDict[str, Visitor] = OrderedDict()
        self._folder = tempfile.TemporaryDirectory(prefix="fbar-visitors-")
        self.root = Path(self._folder.name).resolve()
        # Fails at startup if the inputs don't build, and fills the snap cache before the first visitor.
        probe = self.root / "startup-check"
        Review(self.inputs, probe, root=self.root)
        shutil.rmtree(probe)

    def __len__(self) -> int:
        with self._lock:
            return len(self._visitors)

    def close(self) -> None:
        self._folder.cleanup()

    def _idle(self, visitor: Visitor, now: float) -> bool:
        return now - visitor.last_seen > self.limits.idle_seconds

    def _find(self, token: str | None) -> Visitor | None:
        if token is None:
            return None
        now, expired = self._clock(), None
        with self._lock:
            visitor = self._visitors.get(token)
            if visitor is None:
                return None
            if self._idle(visitor, now):
                expired = self._visitors.pop(token)
            else:
                visitor.last_seen = now
                self._visitors.move_to_end(token)
        if expired:
            expired.retire()
            return None
        return visitor

    def forget(self, token: str | None) -> None:
        """Delete this visitor's worksheet now. Their next request starts a fresh one."""
        with self._lock:
            visitor = self._visitors.pop(token, None) if token else None
        if visitor:
            visitor.retire()

    def _create(self) -> tuple[str, Visitor]:
        # The folder has its own random name, so a token can't be learned from it or the reverse. The
        # build runs outside the store's lock, so other visitors aren't held up by it.
        review = Review(self.inputs, self.root / secrets.token_hex(16), root=self.root)
        token = secrets.token_urlsafe(32)
        now = self._clock()
        visitor = Visitor(review, now)
        evicted = []
        with self._lock:
            self._visitors[token] = visitor
            while self._visitors:
                oldest_token, oldest = next(iter(self._visitors.items()))
                if len(self._visitors) <= self.limits.max_visitors and not self._idle(oldest, now):
                    break
                evicted.append(self._visitors.pop(oldest_token))
        for old in evicted:
            old.retire()
        return token, visitor

    @contextlib.contextmanager
    def visit(self, token: str | None) -> Iterator[tuple[Visitor, str | None]]:
        """Hold the token's visitor for the block, starting a new one if it is unknown. Yields the visitor
        and, if it is new, the token to give the browser."""
        while True:
            new_token = None
            visitor = self._find(token)
            if visitor is None:
                new_token, visitor = self._create()
            with visitor.lock:
                if not visitor.retired:  # otherwise evicted while this request waited: start over
                    yield visitor, new_token
                    return


def _too_long(value, limits: Limits, key: str | None = None) -> bool:
    if isinstance(value, str):
        return len(value) > (limits.max_note_chars if key == "note" else limits.max_field_chars)
    if isinstance(value, dict):
        return any(_too_long(v, limits, k) for k, v in value.items())
    if isinstance(value, list):
        return any(_too_long(v, limits) for v in value)
    return False


class HostedHandler(Handler):
    """Bound to a store and a public host by make_hosted_handler()."""

    review = None  # set per request to the visitor's own Review
    store: VisitorStore
    public_host: str
    _new_token: str | None = None
    _held: list | None = None

    def _own_hosts(self) -> set[str]:
        return {self.public_host}

    def _origin_allowed(self, origin: str | None) -> bool:
        return origin == f"https://{self.public_host}"

    def _from_this_app(self, post: bool) -> bool:
        allowed = super()._from_this_app(post)
        if not allowed:  # the first thing to look at if a new proxy rewrites the Host
            host, origin = self.headers.get("Host", "")[:100], (self.headers.get("Origin") or "")[:100]
            print(f"refused {self.command}: host={host!r} origin={origin!r}", file=sys.stderr, flush=True)
        return allowed

    def _state(self) -> dict:
        return {**super()._state(), "hosted": True}

    def _translate(self, phrases: dict, language: str, fresh: bool = False):
        raise RuntimeError("the hosted demo uses only the reviewed catalog, and this phrase isn't in it")

    def end_headers(self) -> None:
        if self._new_token:
            self.send_header(
                "Set-Cookie", f"{COOKIE}={self._new_token}; Path=/; Secure; HttpOnly; SameSite=Strict"
            )
        super().end_headers()

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        if self._held is not None:
            self._held.append((status, body, ctype))
        else:
            super()._send(status, body, ctype)

    def _token(self) -> str | None:
        for part in self.headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE and TOKEN.fullmatch(value):
                return value
        return None

    @contextlib.contextmanager
    def _visiting(self) -> Iterator[tuple[Visitor, bool]]:
        """Run the block as this request's visitor, then send its response after letting the visitor go:
        a slow client must not keep the visitor locked, since evicting them waits for that lock."""
        self._held = []
        try:
            with self.store.visit(self._token()) as (visitor, new_token):
                self.review, self._new_token = visitor.review, new_token
                yield visitor, new_token is not None
        finally:
            held, self._held = self._held, None
            for response in held:
                self._send(*response)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path not in VISITOR_GETS:
            return super().do_GET()
        if not self._from_this_app(post=False):  # before a visitor can be started
            return self._json(NOT_THIS_APP, HTTPStatus.FORBIDDEN)
        with self._visiting():
            super().do_GET()

    def _read_body(self) -> dict | None:
        """The POST's JSON object, or None once it has been answered with an error."""
        limits = self.store.limits
        length = self.headers.get("Content-Length") or "0"
        if not (length.isascii() and length.isdigit()):
            return self._json({"error": "invalid Content-Length"}, HTTPStatus.BAD_REQUEST)
        if int(length) > limits.max_body_bytes:
            return self._json({"error": "request too large"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        try:
            raw = self.rfile.read(int(length))
        except TimeoutError:
            return self._json({"error": "request timed out"}, HTTPStatus.REQUEST_TIMEOUT)
        try:
            body = json.loads(raw or b"{}")
            too_long = _too_long(body, limits)
        except (ValueError, RecursionError):  # includes bad UTF-8, absurdly long integers and deep nesting
            return self._json({"error": "invalid JSON"}, HTTPStatus.BAD_REQUEST)
        if not isinstance(body, dict):
            return self._json({"error": "expected a JSON object"}, HTTPStatus.BAD_REQUEST)
        if too_long:
            message = (
                f"That text is too long: notes can have {limits.max_note_chars:,} characters "
                f"and other fields {limits.max_field_chars}."
            )
            return self._json({"error": message}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        return body

    def do_POST(self):
        if not self._from_this_app(post=True):
            return self._json(NOT_THIS_APP, HTTPStatus.FORBIDDEN)
        body = self._read_body()
        if body is None:
            return
        if self.path == RESET_PATH:
            self.store.forget(self._token())
            return self._json({"ok": True})
        if self._post_action(body) is None:  # checked first so an unknown path can't start a visitor
            return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        with self._visiting() as (visitor, is_new):
            if is_new:  # never applied to a fresh worksheet the visitor hasn't seen
                return self._json({"error": SESSION_ENDED, "reset": True}, HTTPStatus.CONFLICT)
            if visitor.disk_bytes() > self.store.limits.max_visitor_disk_bytes:
                return self._json({"error": FULL}, HTTPStatus.TOO_MANY_REQUESTS)
            try:
                self._json(*self._run_action(body))
            except (TypeError, ValueError, AttributeError):  # a field of the wrong type
                self._json({"error": "a field has the wrong type"}, HTTPStatus.BAD_REQUEST)


class HostedServer(ThreadingHTTPServer):
    """One thread per connection, up to a fixed number; further connections are closed."""

    request_queue_size = 128  # the default of 5 resets connections when a page's requests arrive together

    def __init__(self, address, handler, max_connections: int):
        self._slots = threading.BoundedSemaphore(max_connections)
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if self._slots.acquire(blocking=False):
            super().process_request(request, client_address)
        else:
            self.shutdown_request(request)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request, client_address):
        if not isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError, TimeoutError)):
            super().handle_error(request, client_address)


def make_hosted_handler(store: VisitorStore, public_host: str) -> type[HostedHandler]:
    return type(
        "BoundHostedHandler",
        (HostedHandler,),
        {
            "store": store,
            "public_host": public_host,
            "assets": asset_index(store.inputs.set_dir),
            "timeout": store.limits.socket_timeout_seconds,
        },
    )


def serve(inputs: Inputs, public_host: str, port: int, limits: Limits | None = None) -> None:
    require_reviewed_catalog()
    store = VisitorStore(inputs, limits)
    server = HostedServer(
        ("0.0.0.0", port), make_hosted_handler(store, public_host), store.limits.max_connections
    )
    print(f"FBAR hosted demo: https://{public_host} (listening on port {port})", flush=True)
    # Plain HTTP by design: the proxy in front terminates HTTPS, as the module docstring says.
    server.serve_forever()  # NOSONAR
