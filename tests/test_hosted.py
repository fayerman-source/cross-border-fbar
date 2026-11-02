"""The hosted demo: its guard, per-visitor isolation, abuse limits and the absence of any model call.

The local guard table at the top also pins behaviour with no new flags: it must hold for the app as it
was before hosted mode existed.
"""

import hashlib
import http.client
import json
import re
import threading
from dataclasses import replace
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from cross_border_fbar import llm
from cross_border_fbar.app import hosted, phrases, server
from cross_border_fbar.app.hosted import BANNER, COOKIE, Limits, VisitorStore, make_hosted_handler
from cross_border_fbar.app.review import Review
from cross_border_fbar.app.server import make_handler
from cross_border_fbar.app.worksheet import Inputs, worksheet_hash

PUBLIC = "demo.example.org"
ORIGIN = f"https://{PUBLIC}"
JSON_TYPE = "application/json"


def request(
    port,
    method,
    path,
    body=None,
    *,
    raw=None,
    host=PUBLIC,
    origin=ORIGIN,
    ctype=JSON_TYPE,
    cookie=None,
    headers=None,
):
    """Send exactly these headers. Returns status, parsed-or-raw body, and the Set-Cookie header."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    sent = {"Host": host} if host is not None else {}
    if method == "POST":
        payload = raw if raw is not None else json.dumps(body if body is not None else {}).encode()
        if origin is not None:
            sent["Origin"] = origin
        if ctype is not None:
            sent["Content-Type"] = ctype
    else:
        payload = None
    if cookie:
        sent["Cookie"] = f"{COOKIE}={cookie}"
    sent.update(headers or {})
    conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
    for k, v in sent.items():
        conn.putheader(k, v)
    if payload is not None and "Content-Length" not in sent:
        conn.putheader("Content-Length", str(len(payload)))
    conn.endheaders(payload)
    res = conn.getresponse()
    data = res.read()
    set_cookie = res.getheader("Set-Cookie")
    conn.close()
    if res.getheader("Content-Type", "").startswith(JSON_TYPE):
        data = json.loads(data)
    return res.status, data, set_cookie


def token_of(set_cookie):
    return re.match(rf"{re.escape(COOKIE)}=([^;]+);", set_cookie).group(1)


@pytest.fixture(scope="session")
def warm_inputs(inputs) -> Inputs:
    """Shares one snap cache, so each store below starts without re-reading every page's pixels."""
    warmed = replace(inputs, snap_cache={})
    VisitorStore(warmed).close()
    return warmed


@pytest.fixture
def start(warm_inputs):
    """start(limits, clock) runs a hosted server on a free port; returns its port and store."""
    running = []

    def _start(limits=None, clock=None, host=PUBLIC):
        store = VisitorStore(warm_inputs, limits, **({"clock": clock} if clock else {}))
        srv = hosted.HostedServer(
            ("127.0.0.1", 0), make_hosted_handler(store, host), store.limits.max_connections
        )
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        running.append((srv, store))
        return srv.server_address[1], store

    yield _start
    for srv, store in running:
        srv.shutdown()
        srv.server_close()
        store.close()


class Browser:
    """One visitor: keeps the cookie the server gives it, as a browser would."""

    def __init__(self, port):
        self.port, self.cookie = port, None

    def _call(self, *args, **kwargs):
        status, data, set_cookie = request(self.port, *args, cookie=self.cookie, **kwargs)
        if set_cookie:
            self.cookie = token_of(set_cookie)
        return status, data

    def state(self):
        status, data = self._call("GET", "/api/state")
        assert status == 200
        return data

    def get(self, path):
        return self._call("GET", path)

    def post(self, path, body=None, **kwargs):
        return self._call("POST", path, body, **kwargs)


def sign_off_everything(browser, name="A. Rivera"):
    for flag in browser.state()["open_review_flags"]:
        assert (
            browser.post("/api/ack", {"flag": flag, "note": f"checked by {name}", "reviewer": name})[0] == 200
        )
    status, view = browser.post("/api/signoff", {"reviewer": name, "initials": "AR"})
    assert status == 200
    return view


def correctable(view):
    upload, page = next((u, p) for u, p in view["page_readings"].items() if p["rows"])
    return upload, page["rows"][0]["balance"]["field"]


# -- local mode, no flags ------------------------------------------------------------------------------


@pytest.fixture
def local(inputs, tmp_path):
    review = Review(inputs, tmp_path / "r", root=tmp_path)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(review, inputs.set_dir))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def test_local_guard_is_unchanged(local):
    here = f"127.0.0.1:{local}"
    named = f"localhost:{local}"
    gets = [
        (here, 200),
        (named, 200),
        (f"127.0.0.1:{local + 1}", 403),
        ("evil.example", 403),
        (PUBLIC, 403),
        ("", 403),
    ]
    for host, expected in gets:
        assert request(local, "GET", "/api/state", host=host)[0] == expected, host
    posts = [  # with no flag id in the body, a request that passes the guard stops at 400 missing field
        (dict(host=here, origin=None), 400),
        (dict(host=here, origin=f"http://{here}"), 400),
        (dict(host=here, origin=f"http://{named}"), 400),
        (dict(host=here, origin=f"https://{here}"), 403),
        (dict(host=here, origin=f"http://{here}/path"), 403),
        (dict(host=here, origin="http://evil.example"), 403),
        (dict(host=here, origin="null"), 403),
        (dict(host=here, origin=None, ctype="text/plain"), 403),
        (dict(host=here, origin=None, ctype=None), 403),
        (dict(host=here, origin=None, ctype="application/json; charset=utf-8"), 400),
        (dict(host="evil.example", origin=None), 403),
        (dict(host=PUBLIC, origin=ORIGIN), 403),
    ]
    for kwargs, expected in posts:
        assert request(local, "POST", "/api/ack", {}, **kwargs)[0] == expected, kwargs


def test_local_mode_has_no_banner_and_no_cookie(local):
    status, page, set_cookie = request(local, "GET", "/", host=f"127.0.0.1:{local}")
    assert status == 200 and b"demo-banner" not in page and set_cookie is None
    assert request(local, "GET", "/api/state", host=f"127.0.0.1:{local}")[2] is None
    index = Path(server.STATIC / "index.html").read_bytes()
    assert page == index


def test_main_with_no_flags_binds_loopback_on_the_usual_port_whatever_the_host_environment(
    inputs, tmp_path, monkeypatch
):
    bound = {}

    class Fake:
        def __init__(self, address, handler):
            bound.update(address=address, hosted=issubclass(handler, hosted.HostedHandler))

        def serve_forever(self):
            pass

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(server, "ThreadingHTTPServer", Fake)
    monkeypatch.setenv("SPACE_HOST", PUBLIC)
    monkeypatch.setenv("FBAR_PUBLIC_HOST", PUBLIC)
    monkeypatch.setenv("PORT", "9999")
    argv = ["app", "--set", str(inputs.set_dir), "--readings", str(inputs.readings)]
    monkeypatch.setattr("sys.argv", argv)
    server.main()
    assert bound == {"address": ("127.0.0.1", 8765), "hosted": False}


# -- starting hosted mode ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        ["--hosted"],
        ["--public-host", PUBLIC],
        ["--hosted", "--public-host", f"https://{PUBLIC}"],
        ["--hosted", "--public-host", f"{PUBLIC}/path"],
        ["--hosted", "--public-host", "*"],
        ["--hosted", "--public-host", "a b"],
        ["--hosted", "--public-host", PUBLIC, "--review-dir", "somewhere"],
    ],
)
def test_hosted_mode_fails_closed(args, monkeypatch):
    for name in ("SPACE_HOST", "FBAR_PUBLIC_HOST"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(hosted, "serve", lambda *a: pytest.fail("started"))
    monkeypatch.setattr("sys.argv", ["app", *args])
    with pytest.raises(SystemExit) as e:
        server.main()
    assert e.value.code == 2


def test_public_host_sources_in_order():
    assert (
        hosted.public_host_from("a.example", {"FBAR_PUBLIC_HOST": "b.example", "SPACE_HOST": "c.example"})
        == "a.example"
    )
    assert (
        hosted.public_host_from(None, {"FBAR_PUBLIC_HOST": "b.example", "SPACE_HOST": "c.example"})
        == "b.example"
    )
    assert hosted.public_host_from(None, {"SPACE_HOST": "user-demo.hf.space"}) == "user-demo.hf.space"
    assert hosted.public_host_from("Demo.Example:8443", {}) == "demo.example:8443"


def test_hosted_main_defaults_to_port_7860_and_honours_the_environment(inputs, monkeypatch):
    calls = []
    monkeypatch.setattr(hosted, "serve", lambda _inputs, host, port: calls.append((host, port)))
    monkeypatch.setattr("sys.argv", ["app", "--hosted", "--public-host", PUBLIC])
    monkeypatch.delenv("PORT", raising=False)
    server.main()
    monkeypatch.setenv("PORT", "8080")
    server.main()
    monkeypatch.setattr("sys.argv", ["app", "--hosted", "--public-host", PUBLIC, "--port", "9000"])
    server.main()
    assert calls == [(PUBLIC, 7860), (PUBLIC, 8080), (PUBLIC, 9000)]


# -- the guard -----------------------------------------------------------------------------------------


def test_hosted_guard_refuses_any_other_host_or_origin(start):
    port, store = start()
    for path in ("/", "/api/state", "/static/app.js"):
        for host in ("evil.example", f"127.0.0.1:{port}", f"localhost:{port}", f"{PUBLIC}.evil.example", ""):
            assert request(port, "GET", path, host=host)[0] == 403, (path, host)
        spoofed = {
            "X-Forwarded-Host": PUBLIC,
            "X-Forwarded-Proto": "https",
            "Forwarded": f"host={PUBLIC};proto=https",
        }
        assert request(port, "GET", path, host="evil.example", headers=spoofed)[0] == 403
    assert len(store) == 0  # a refused request never starts a visitor

    status, _, set_cookie = request(port, "GET", "/api/state")
    assert status == 200
    cookie = token_of(set_cookie)
    flag = store.inputs and request(port, "GET", "/api/state", cookie=cookie)[1]["open_review_flags"][0]
    ack = {"flag": flag, "note": "checked", "reviewer": "A"}
    refused = [
        dict(origin=None),  # hosted requires it: browsers always send it on a same-origin POST
        dict(origin=f"http://{PUBLIC}"),
        dict(origin=f"https://{PUBLIC}:443"),
        dict(origin=f"https://{PUBLIC}/"),
        dict(origin=f"https://{PUBLIC}.evil.example"),
        dict(origin="https://evil.example"),
        dict(origin="null"),
        dict(ctype="text/plain"),
        dict(ctype=None),
        dict(host="evil.example"),
        dict(origin=f"http://{PUBLIC}", headers={"X-Forwarded-Proto": "https"}),
        dict(host="evil.example", headers={"X-Forwarded-Host": PUBLIC}),
    ]
    for kwargs in refused:
        assert request(port, "POST", "/api/ack", ack, cookie=cookie, **kwargs)[0] == 403, kwargs
    view = request(port, "GET", "/api/state", cookie=cookie)[1]
    assert flag in view["open_review_flags"] and view["review"]["acks"] == {}
    assert request(port, "POST", "/api/ack", ack, cookie=cookie)[0] == 200


def test_the_visitor_cookie_is_host_only_secure_http_only_and_strict(start):
    port, _ = start()
    _, _, set_cookie = request(port, "GET", "/api/state")
    attributes = {a.strip().lower() for a in set_cookie.split(";")[1:]}
    assert set_cookie.startswith(f"{COOKIE}=")
    assert attributes == {"path=/", "secure", "httponly", "samesite=strict"}


def test_pages_and_assets_set_no_cookie_and_the_app_still_refuses_framing(start):
    port, store = start()
    for path in ("/", "/static/app.js", "/static/style.css"):
        assert request(port, "GET", path)[2] is None
    assert len(store) == 0
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    conn.request("GET", "/", headers={"Host": PUBLIC})
    res = conn.getresponse()
    res.read()
    conn.close()
    assert "frame-ancestors 'none'" in res.getheader("Content-Security-Policy")


# -- isolation -----------------------------------------------------------------------------------------


def test_one_visitor_never_sees_anothers_acknowledgement_correction_or_sign_off(start, warm_inputs):
    port, _ = start()
    a, b = Browser(port), Browser(port)
    baseline = a.state()
    assert baseline["worksheet"]["totals"]["aggregate_usd"] == 373_618
    assert b.state() == baseline
    assert a.cookie != b.cookie
    protected = [warm_inputs.readings, warm_inputs.intake]
    before = [hashlib.sha256(p.read_bytes()).hexdigest() for p in protected]

    flag = baseline["open_review_flags"][0]
    assert a.post("/api/ack", {"flag": flag, "note": "PRIVATE-NOTE-A", "reviewer": "Visitor A"})[0] == 200
    upload, field = correctable(baseline)
    status, _ = a.post(
        "/api/value",
        {
            "upload": upload,
            "field": field,
            "value": "999,999,999",
            "note": "PRIVATE-FIX-A",
            "reviewer": "Visitor A",
        },
    )
    assert status == 200
    assert a.state()["worksheet"]["totals"]["aggregate_usd"] != 373_618
    view_a = sign_off_everything(a, "Visitor A")
    assert view_a["review"]["signoff"]["reviewer"] == "Visitor A"

    seen_by_b = b.state()
    assert seen_by_b == baseline
    assert "PRIVATE" not in json.dumps(seen_by_b) and "Visitor A" not in json.dumps(seen_by_b)
    assert b.get("/api/export")[0] == 409
    assert a.get("/api/export")[0] == 200

    # B's own work goes on undisturbed, and doesn't reach A.
    assert b.post("/api/ack", {"flag": flag, "note": "from B", "reviewer": "Visitor B"})[0] == 200
    assert "from B" not in json.dumps(a.state())
    assert a.state()["review"]["signoff"]["reviewer"] == "Visitor A"

    # A newcomer gets the original worksheet, and nothing shared was written to.
    assert Browser(port).state() == baseline
    assert [hashlib.sha256(p.read_bytes()).hexdigest() for p in protected] == before
    assert worksheet_hash(Browser(port).state()["worksheet"]) == worksheet_hash(baseline["worksheet"])


def test_visitors_have_separate_folders_not_named_after_their_tokens(start):
    port, store = start()
    a, b = Browser(port), Browser(port)
    a.state(), b.state()
    folders = [p.name for p in store.root.iterdir()]
    assert len(folders) == 2
    assert a.cookie not in folders and b.cookie not in folders


def test_concurrent_requests_stay_with_their_own_visitor(start):
    port, _ = start()
    browsers = [Browser(port) for _ in range(4)]
    views = [b.state() for b in browsers]
    errors = []

    def work(index):
        browser, flags = browsers[index], views[index]["open_review_flags"]
        try:
            threads = [
                threading.Thread(
                    target=lambda f=f: browser.post(
                        "/api/ack", {"flag": f, "note": f"visitor-{index}", "reviewer": "R"}
                    )
                )
                for f in flags
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            view = browser.state()
            notes = {ack["note"] for ack in view["review"]["acks"].values()}
            if notes != {f"visitor-{index}"} or view["open_review_flags"]:
                errors.append((index, notes, view["open_review_flags"]))
        except Exception as e:  # noqa: BLE001 - reported below; a thread's failure wouldn't fail the test
            errors.append((index, repr(e)))

    workers = [threading.Thread(target=work, args=(i,)) for i in range(4)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    assert errors == []


# -- eviction ------------------------------------------------------------------------------------------


def test_least_recently_used_visitor_is_evicted_and_returns_to_a_fresh_worksheet(start):
    port, store = start(Limits(max_visitors=2))
    a, b = Browser(port), Browser(port)
    baseline = a.state()
    flag = baseline["open_review_flags"][0]
    assert a.post("/api/ack", {"flag": flag, "note": "A's note", "reviewer": "A"})[0] == 200
    b.state()
    a.state()  # A is now the most recently used, so B is next to go
    c = Browser(port)
    c.state()
    assert len(store) == 2
    assert len(list(store.root.iterdir())) == 2  # the evicted visitor's folder is gone
    assert a.state()["review"]["acks"]  # A kept its work

    old = b.cookie
    assert b.state() == baseline  # a clean restart with a new cookie, not an error
    assert b.cookie != old


def test_a_post_from_an_evicted_visitor_resets_and_is_not_applied(start):
    port, store = start(Limits(max_visitors=1))
    a = Browser(port)
    flag = a.state()["open_review_flags"][0]
    Browser(port).state()  # evicts A
    old = a.cookie
    status, data = a.post("/api/ack", {"flag": flag, "note": "late", "reviewer": "A"})
    assert status == 409 and data["reset"] is True
    assert a.cookie != old
    view = a.state()
    assert view["review"]["acks"] == {} and flag in view["open_review_flags"]
    assert a.post("/api/ack", {"flag": flag, "note": "again", "reviewer": "A"})[0] == 200
    assert len(store) == 1


def test_idle_visitors_expire(start):
    now = [1000.0]
    port, store = start(Limits(idle_seconds=60), clock=lambda: now[0])
    a, b = Browser(port), Browser(port)
    flag = a.state()["open_review_flags"][0]
    b.state()
    assert a.post("/api/ack", {"flag": flag, "note": "n", "reviewer": "A"})[0] == 200
    now[0] += 59
    assert a.state()["review"]["acks"]  # active, so kept
    now[0] += 59  # A used 59s ago; B idle for 118s
    Browser(port).state()  # arriving sweeps B out
    assert len(store) == 2
    now[0] += 61
    old = a.cookie
    view = a.state()  # returning after expiry
    assert a.cookie != old and view["review"]["acks"] == {}
    assert len(list(store.root.iterdir())) <= 2


def test_eviction_waits_for_a_request_in_progress(warm_inputs):
    store = VisitorStore(warm_inputs, Limits(max_visitors=1))
    entered, release = threading.Event(), threading.Event()
    results = {}

    def slow_request():
        with store.visit(None) as (visitor, _):
            entered.set()
            release.wait(30)
            # The folder must still be there for the audit write at the end of an action.
            flag = visitor.review.open_review_flags()[0]
            visitor.review.acknowledge(flag, "still here", "A")
            results["ack"] = visitor.review.audit_path.exists()
            results["dir"] = visitor.review.dir

    first = threading.Thread(target=slow_request)
    first.start()
    assert entered.wait(30)
    second = threading.Thread(target=lambda: store.visit(None).__enter__())
    second.start()
    second.join(2)
    assert second.is_alive()  # waiting for the first request to finish, not deleting under it
    release.set()
    first.join(30)
    second.join(30)
    assert results["ack"] is True
    assert not results["dir"].exists()  # deleted once the request was done
    store.close()


def test_visitor_count_and_folders_stay_capped_under_a_stream_of_new_visitors(start):
    port, store = start(Limits(max_visitors=3))
    for _ in range(8):
        assert request(port, "GET", "/api/state")[0] == 200
    assert len(store) == 3
    assert len(list(store.root.iterdir())) == 3


# -- request limits ------------------------------------------------------------------------------------


def test_post_limits(start):
    port, store = start(Limits(max_body_bytes=4096, max_note_chars=100, max_field_chars=20))
    a = Browser(port)
    flag = a.state()["open_review_flags"][0]
    ack = {"flag": flag, "reviewer": "A"}
    audit = store._visitors[a.cookie].review.audit_path
    assert a.post("/api/ack", {**ack, "note": "x" * 5000})[0] == 413  # over the body cap
    assert a.post("/api/ack", {**ack, "note": "x" * 101})[0] == 413  # over the note cap
    assert a.post("/api/ack", {**ack, "note": "ok", "reviewer": "x" * 21})[0] == 413  # over a field cap
    assert a.post("/api/ack", {**ack, "note": "ok", "rate": {"source": "x" * 21}})[0] == 413  # nested
    assert not audit.exists()
    assert a.post("/api/ack", {**ack, "note": "x" * 100})[0] == 200

    for kwargs in (
        dict(raw=b"not json"),
        dict(raw=b"\xff\xfe"),
        dict(raw=b"[1,2]"),
        dict(raw=b'"text"'),
        dict(raw=b"[" * 2000),
        dict(raw=b"1" * 3000),
        dict(raw=b'{"a":' + b"[" * 800 + b"]" * 800 + b"}"),
    ):
        assert a.post("/api/ack", **kwargs)[0] == 400, kwargs
    for length in ("-1", "abc", "1e3", "٣"):
        raw_length = {"Content-Length": length.encode().decode("latin-1")}
        assert a.post("/api/ack", raw=b"{}", headers=raw_length)[0] == 400, length
    assert a.post("/api/ack", raw=b"{}", headers={"Content-Length": "99999999"})[0] == 413


def test_wrong_field_types_get_an_answer_rather_than_a_dropped_connection(start):
    port, _ = start()
    a = Browser(port)
    flag = a.state()["open_review_flags"][0]
    assert a.post("/api/ack", {"flag": flag, "note": "n", "reviewer": "A"})[0] == 200
    for path, body in (
        ("/api/reopen", {"flag": ["not", "hashable"]}),
        ("/api/ack", {"flag": {"a": 1}, "note": "n"}),
        ("/api/ack", {"flag": None, "note": "n", "rate": 5}),
        ("/api/ownership", {"account": [], "ownership": "sole", "note": "n"}),
        ("/api/currency", {"account": {}, "currency": [], "note": "n"}),
        ("/api/value", {"upload": 1, "field": 2, "value": 3, "note": "n"}),
        ("/api/summary/approve", {"release": []}),
    ):
        assert a.post(path, body)[0] in (400, 409), path
    assert list(a.state()["review"]["acks"]) == [flag]


def test_each_visitor_has_a_disk_quota(start):
    limit = 8 * 1024
    port, store = start(Limits(max_visitor_disk_bytes=limit))
    a, b = Browser(port), Browser(port)
    flag = a.state()["open_review_flags"][0]
    b.state()
    statuses = [
        a.post("/api/reopen" if i % 2 else "/api/ack", {"flag": flag, "note": "x" * 1500, "reviewer": "A"})[0]
        for i in range(100)
    ]
    assert statuses[0] == 200 and statuses[-1] == 429
    visitor = store._visitors[a.cookie]
    assert visitor.disk_bytes() < limit + 16 * 1024  # stops at the limit, give or take one request
    assert a.state()["worksheet"]["totals"]["aggregate_usd"] == 373_618  # still readable
    assert b.post("/api/ack", {"flag": flag, "note": "n", "reviewer": "B"})[0] == 200  # B is unaffected


def test_a_full_server_closes_extra_connections_and_recovers(warm_inputs):
    import socket

    store = VisitorStore(warm_inputs)
    srv = hosted.HostedServer(("127.0.0.1", 0), make_hosted_handler(store, PUBLIC), max_connections=2)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        held = [socket.create_connection(("127.0.0.1", port)) for _ in range(2)]
        for s in held:
            s.settimeout(5)
        extra = socket.create_connection(("127.0.0.1", port))
        extra.settimeout(5)
        assert extra.recv(1) == b""  # closed at once, with no response
        extra.close()
        for s in held:
            s.close()
        for _ in range(100):  # the handler threads notice the closed sockets and give their slots back
            try:
                if request(port, "GET", "/static/app.js")[0] == 200:
                    break
            except (http.client.HTTPException, OSError):
                continue
        assert request(port, "GET", "/static/app.js")[0] == 200
    finally:
        srv.shutdown()
        srv.server_close()
        store.close()


def test_a_stalled_upload_times_out(start):
    import socket

    port, _ = start(Limits(socket_timeout_seconds=0.2))
    s = socket.create_connection(("127.0.0.1", port))
    s.settimeout(10)
    s.sendall(
        f"POST /api/ack HTTP/1.1\r\nHost: {PUBLIC}\r\nOrigin: {ORIGIN}\r\nContent-Type: application/json\r\n"
        "Content-Length: 100\r\n\r\n{".encode()
    )
    assert b"408" in s.recv(200).split(b"\r\n")[0]
    s.close()


# -- files ---------------------------------------------------------------------------------------------


def test_traversal_finds_nothing_and_a_cookie_never_names_a_file(start, warm_inputs):
    port, store = start()
    page = next((warm_inputs.set_dir / "pages").glob("*.png")).name
    status, _, _ = request(port, "GET", f"/doc/pages/{page}")
    assert status == 200
    for path in (
        "/doc/pages/../intake.json",
        "/doc/pages/../truth.json",
        "/doc/pages/..%2f..%2ftruth.json",
        "/doc/pages/%2e%2e/truth.json",
        "/doc/pages/....//truth.json",
        "/doc/pages//etc/passwd",
        "/doc/../../../../etc/passwd",
        "/static/../hosted.py",
        "/static/..%2fhosted.py",
        "/fonts/../rates.py",
        f"/doc/pages/{page}/../../truth.json",
        "/doc/truth.json",
        "/truth.json",
    ):
        status, body, _ = request(port, "GET", path)
        assert status == 404 and body == {"error": "not found"}, path

    for token in ("../../etc/passwd", "..%2f..", "A" * 43, "x" * 5000, ""):
        status, body, set_cookie = request(port, "GET", "/api/state", cookie=token)
        assert status == 200 and set_cookie  # treated as a new visitor
    names = {p.name for p in store.root.iterdir()}
    assert all(re.fullmatch(r"[0-9a-f]{32}", name) for name in names)


# -- no model calls ------------------------------------------------------------------------------------


@pytest.fixture
def no_model(monkeypatch):
    """Any route to a model fails the test, with a key in the environment as if one had leaked in."""

    def refuse(*a, **k):
        raise AssertionError("a model call was attempted in hosted mode")

    monkeypatch.setenv("MODEL_API_KEY", "placeholder-not-a-real-value")
    monkeypatch.setattr(llm.Client, "__init__", refuse)
    monkeypatch.setattr(llm.Client, "chat", refuse)
    monkeypatch.setattr(llm.Client, "_post", refuse)
    monkeypatch.setattr(llm.urllib.request, "urlopen", refuse)
    monkeypatch.setattr(server, "Client", refuse)
    monkeypatch.setattr(server, "translate_phrases", refuse)


def test_full_walkthrough_uses_no_model_and_only_reviewed_korean(start, no_model):
    port, _ = start()
    a = Browser(port)
    assert a.state()["worksheet"]["totals"]["aggregate_usd"] == 373_618
    sign_off_everything(a)
    status, view = a.post("/api/summary/draft")
    assert status == 200
    summary = view["review"]["summary"]
    assert summary["blocked"] is None and summary["unreviewed"] == {} and summary["meta"]["model"] is None
    assert summary["text"]
    status, view = a.post("/api/summary/approve", {"reviewer": "A", "release": "ko"})
    assert status == 200 and view["review"]["summary"]["approved"]["release"] == "ko"


def test_a_phrase_missing_from_the_catalog_is_refused_not_sent_to_a_model(
    start, no_model, tmp_path, monkeypatch
):
    port, _ = start()
    a = Browser(port)
    sign_off_everything(a)
    catalog = phrases.load("ko")
    english = phrases.current_phrases()["heading"]
    catalog.delete(english, context="heading")
    monkeypatch.setattr(phrases, "PHRASES_DIR", tmp_path / "catalog")
    phrases.save("ko", catalog)

    status, view = a.post("/api/summary/draft")
    assert status == 200
    summary = view["review"]["summary"]
    assert "reviewed catalog" in summary["blocked"]
    assert summary["text"] is None and summary["text_en"] and summary["unreviewed"] == {}
    assert all(line["ko"] is None for line in summary["lines"])
    assert a.post("/api/summary/approve", {"reviewer": "A", "release": "ko"})[0] == 409
    assert (
        a.post("/api/summary/approve", {"reviewer": "A", "release": "en"})[0] == 200
    )  # English needs no model


def test_hosted_mode_will_not_start_with_an_unreviewed_phrase(heading_unreviewed):
    with pytest.raises(SystemExit, match="heading"):
        hosted.require_reviewed_catalog()


def test_the_committed_catalog_lets_hosted_mode_start():
    hosted.require_reviewed_catalog()


# -- banner --------------------------------------------------------------------------------------------


def test_the_banner_is_on_the_page_in_plain_words(start):
    port, _ = start()
    status, page, _ = request(port, "GET", "/")
    assert status == 200
    assert f'<p class="demo-banner" role="note">{BANNER.replace(chr(39), "&#x27;")}</p>'.encode() in page
    assert request(port, "GET", "/index.html")[1] == page
    assert request(port, "GET", "/static/index.html")[1] == page
    for words in ("fictional", "recorded", "private", "temporary", "real client information"):
        assert words in BANNER
    assert "—" not in BANNER and "–" not in BANNER
    assert not re.search(r"[가-힣]", BANNER)


def test_snap_cache_gives_the_same_worksheet_and_each_build_its_own_copy(inputs, warm_inputs, tmp_path):
    plain = Review(inputs, tmp_path / "plain", root=tmp_path)
    cached = Review(warm_inputs, tmp_path / "cached", root=tmp_path)
    assert worksheet_hash(plain.worksheet) == worksheet_hash(cached.worksheet)
    image, snapped = next((k, v) for k, v in warm_inputs.snap_cache.items() if v)
    from cross_border_fbar.app.worksheet import _snapped

    copy_of = _snapped(warm_inputs, {}, image)
    copy_of["rows"].clear()
    assert warm_inputs.snap_cache[image] == snapped and snapped["rows"]
