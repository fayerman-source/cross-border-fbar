import json

import pytest

from cross_border_fbar import llm


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    return llm.Client(mode="auto", cap_usd=1.0, ledger=tmp_path / "ledger.jsonl", recordings=tmp_path / "rec")


def fake_response(content="{}", completion_tokens=100):
    return {
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1000, "completion_tokens": completion_tokens},
    }


@pytest.mark.parametrize("model", ["muse-spark-1.3-contributor", "muse-spark-1.2-contributor", "gpt-4o"])
def test_non_standard_models_are_refused_before_any_request(client, model, monkeypatch):
    monkeypatch.setattr(client, "_post", lambda body: pytest.fail("request was sent"))
    with pytest.raises(ValueError):
        client.chat(model, [{"role": "user", "content": "hi"}], max_tokens=10)


def test_cap_refuses_a_call_that_could_exceed_it(client, monkeypatch):
    monkeypatch.setattr(client, "_post", lambda body: pytest.fail("request was sent"))
    client.ledger.write_text(json.dumps({"cost_usd": 0.99}) + "\n", encoding="utf-8")
    with pytest.raises(llm.SpendCapExceeded):
        client.chat("muse-spark-1.3", [{"role": "user", "content": "hi"}], max_tokens=16000)


def test_live_call_is_recorded_then_replayed_without_network(client, monkeypatch):
    calls = []
    monkeypatch.setattr(client, "_post", lambda body: calls.append(body) or fake_response('{"a": 1}'))
    msgs = [{"role": "user", "content": "read this"}]
    first = client.chat("muse-spark-1.3", msgs, max_tokens=100)
    assert not first.replayed
    assert len(calls) == 1
    assert client.spent_usd() == pytest.approx(
        llm.cost_usd("muse-spark-1.3", fake_response()["usage"]), abs=1e-6
    )

    offline = llm.Client(mode="replay", ledger=client.ledger, recordings=client.recordings)
    monkeypatch.setattr(offline, "_post", lambda body: pytest.fail("replay hit the network"))
    again = offline.chat("muse-spark-1.3", msgs, max_tokens=100)
    assert again.replayed
    assert again.content == '{"a": 1}'
    assert again.key == first.key


def test_replay_mode_fails_loudly_on_unrecorded_requests(client):
    offline = llm.Client(mode="replay", ledger=client.ledger, recordings=client.recordings)
    with pytest.raises(llm.NotRecorded):
        offline.chat("muse-spark-1.3", [{"role": "user", "content": "new"}], max_tokens=100)


def test_recordings_never_contain_the_key(client, monkeypatch):
    marker = "-".join(["fake", "key", "marker"])  # built at runtime so it never looks like a credential
    monkeypatch.setenv("MODEL_API_KEY", marker)
    monkeypatch.setattr(client, "_post", lambda body: fake_response())
    client.chat("muse-spark-1.3", [{"role": "user", "content": "x"}], max_tokens=10)
    stored = "".join(
        p.read_text(encoding="utf-8") for p in client.recordings.rglob("*.json")
    ) + client.ledger.read_text(encoding="utf-8")
    assert marker not in stored


def test_concurrent_calls_cannot_overshoot_the_cap(tmp_path, monkeypatch):
    """Codex review on PR #2: four threads all passed the check before any cost was recorded."""
    import threading
    import time

    ledger = tmp_path / "ledger.jsonl"
    c = llm.Client(mode="live", cap_usd=0.10, ledger=ledger, recordings=tmp_path / "rec")
    in_flight, peak = [0], [0]
    lock = threading.Lock()

    def slow_post(body):
        with lock:
            in_flight[0] += 1
            peak[0] = max(peak[0], in_flight[0])
        time.sleep(0.2)
        with lock:
            in_flight[0] -= 1
        return fake_response(completion_tokens=15_000)  # about $0.065 each

    monkeypatch.setattr(c, "_post", slow_post)
    results = []

    def call(i):
        try:
            c.chat("muse-spark-1.3", [{"role": "user", "content": f"page {i}"}], max_tokens=16_000)
            results.append("ok")
        except llm.SpendCapExceeded:
            results.append("refused")

    threads = [threading.Thread(target=call, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak[0] == 1  # the worst case of one call (~$0.068) leaves no room for a second
    assert c.spent_usd() <= 0.10
    assert results.count("ok") == 1


def test_failed_call_keeps_its_reservation_until_cost_is_known(client, monkeypatch):
    def boom(body):
        raise RuntimeError("HTTP 500")

    monkeypatch.setattr(client, "_post", boom)
    with pytest.raises(RuntimeError):
        client.chat("muse-spark-1.3", [{"role": "user", "content": "x"}], max_tokens=100)
    assert client.spent_usd() > 0
    assert json.loads(client.ledger.read_text(encoding="utf-8").splitlines()[0])["kind"] == "reservation"


@pytest.mark.parametrize(
    "usage",
    [
        None,
        {},
        {"prompt_tokens": True, "completion_tokens": 1},
        {"prompt_tokens": 1, "completion_tokens": -1},
        {"prompt_tokens": 1, "completion_tokens": 1, "prompt_tokens_details": {"cached_tokens": 2}},
    ],
)
def test_invalid_usage_keeps_reservation_and_does_not_record_a_replay(client, monkeypatch, usage):
    response = fake_response()
    response["usage"] = usage
    monkeypatch.setattr(client, "_post", lambda body: response)
    with pytest.raises(ValueError, match="reservation retained"):
        client.chat("muse-spark-1.3", [{"role": "user", "content": "x"}], max_tokens=100)
    assert client.spent_usd() > 0
    assert not list(client.recordings.rglob("*.json"))


def test_self_hosted_model_never_receives_the_api_key(client, monkeypatch):
    sent = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(fake_response()).encode()

    def fake_urlopen(req, timeout):
        sent["url"], sent["headers"] = req.full_url, dict(req.header_items())
        return FakeResp()

    monkeypatch.setenv("MODEL_API_KEY", "-".join(["should", "not", "leak"]))
    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    r = client.chat("muse-glimmer", [{"role": "user", "content": "x"}], max_tokens=10)
    assert sent["url"].startswith(llm.SELF_HOSTED_URL)
    assert "Authorization" not in sent["headers"]
    assert r.cost_usd == 0
