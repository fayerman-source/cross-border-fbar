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
    client.ledger.write_text(json.dumps({"cost_usd": 0.99}) + "\n")
    with pytest.raises(llm.SpendCapExceeded):
        client.chat("muse-spark-1.3", [{"role": "user", "content": "hi"}], max_tokens=16000)


def test_live_call_is_recorded_then_replayed_without_network(client, monkeypatch):
    calls = []
    monkeypatch.setattr(client, "_post", lambda body: calls.append(body) or fake_response('{"a": 1}'))
    msgs = [{"role": "user", "content": "read this"}]
    first = client.chat("muse-spark-1.3", msgs, max_tokens=100)
    assert not first.replayed and len(calls) == 1
    assert client.spent_usd() == pytest.approx(
        llm.cost_usd("muse-spark-1.3", fake_response()["usage"]), abs=1e-6
    )

    offline = llm.Client(mode="replay", ledger=client.ledger, recordings=client.recordings)
    monkeypatch.setattr(offline, "_post", lambda body: pytest.fail("replay hit the network"))
    again = offline.chat("muse-spark-1.3", msgs, max_tokens=100)
    assert again.replayed and again.content == '{"a": 1}' and again.key == first.key


def test_replay_mode_fails_loudly_on_unrecorded_requests(client):
    offline = llm.Client(mode="replay", ledger=client.ledger, recordings=client.recordings)
    with pytest.raises(llm.NotRecorded):
        offline.chat("muse-spark-1.3", [{"role": "user", "content": "new"}], max_tokens=100)


def test_recordings_never_contain_the_key(client, monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "LLM_123_secret-value")
    monkeypatch.setattr(client, "_post", lambda body: fake_response())
    client.chat("muse-spark-1.3", [{"role": "user", "content": "x"}], max_tokens=10)
    stored = "".join(p.read_text() for p in client.recordings.rglob("*.json")) + client.ledger.read_text()
    assert "secret-value" not in stored
