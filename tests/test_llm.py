"""The optional LLM providers: exact requests, replies and errors (no network is used)."""

import io
import json
import urllib.error

import pytest

from autoshelf import llm
from autoshelf.llm import base, claude, ollama, secrets
from autoshelf.llm.base import LLMError, Message


class Recorder:
    """A transport that records the request and returns a canned reply."""

    def __init__(self, status: int, reply) -> None:
        self.status = status
        self.reply = reply
        self.requests: list[tuple] = []

    def __call__(self, url, headers, body, timeout):
        self.requests.append((url, headers, body, timeout))
        return self.status, self.reply


def test_claude_request_follows_the_messages_api() -> None:
    transport = Recorder(
        200,
        {
            "type": "message",
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "let me think", "signature": "x"},
                {"type": "text", "text": "Hello "},
                {"type": "text", "text": "there."},
            ],
            "stop_reason": "end_turn",
        },
    )
    provider = claude.ClaudeProvider("sk-test", transport=transport)

    reply = provider.complete("Be brief.", [Message("user", "Hi")], max_tokens=100)

    assert reply == "Hello there."  # thinking blocks are left out
    ((url, headers, body, _),) = transport.requests
    assert url == "https://api.anthropic.com/v1/messages"
    assert headers == {
        "x-api-key": "sk-test",
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    assert body == {
        "model": "claude-opus-5-5",
        "max_tokens": claude.MIN_MAX_TOKENS,  # room for thinking
        "system": "Be brief.",
        "messages": [{"role": "user", "content": "Hi"}],
    }


def test_claude_errors_show_the_apis_message() -> None:
    transport = Recorder(
        401,
        {
            "type": "error",
            "error": {"type": "authentication_error", "message": "invalid x-api-key"},
        },
    )
    provider = claude.ClaudeProvider("bad", model="claude-haiku-5-5", transport=transport)
    with pytest.raises(LLMError, match=r"401.*authentication_error: invalid x-api-key"):
        provider.complete("s", [Message("user", "Hi")])
    assert transport.requests[0][2]["model"] == "claude-haiku-5-5"


def test_claude_needs_a_key_and_an_answer() -> None:
    with pytest.raises(LLMError, match="No Claude API key"):
        claude.ClaudeProvider("")
    empty = claude.ClaudeProvider(
        "k", transport=Recorder(200, {"content": [], "stop_reason": "max_tokens"})
    )
    with pytest.raises(LLMError, match="max_tokens"):
        empty.complete("s", [Message("user", "Hi")])


def test_ollama_request_and_errors() -> None:
    transport = Recorder(200, {"message": {"role": "assistant", "content": " Hi! "}, "done": True})
    provider = ollama.OllamaProvider("http://localhost:11434/", "llama3.2", transport=transport)

    assert provider.complete("Be brief.", [Message("user", "Hello")], max_tokens=50) == "Hi!"

    ((url, _, body, _),) = transport.requests
    assert url == "http://localhost:11434/api/chat"
    assert body["model"] == "llama3.2"
    assert body["stream"] is False
    assert body["messages"] == [
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": "Hello"},
    ]
    missing = ollama.OllamaProvider(transport=Recorder(404, {"error": "model 'x' not found"}))
    with pytest.raises(LLMError, match="model 'x' not found"):
        missing.complete("s", [Message("user", "Hi")])


class FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def test_post_json_round_trip_and_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = []

    def answer(request, timeout):
        seen.append((request.full_url, request.get_method(), json.loads(request.data)))
        return FakeResponse(json.dumps({"ok": True}).encode())

    monkeypatch.setattr(base, "_urlopen", answer)
    assert base.post_json("https://example.test/x", {}, {"a": 1}, 5) == (200, {"ok": True})
    assert seen == [("https://example.test/x", "POST", {"a": 1})]

    def http_error(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, 500, "boom", {}, io.BytesIO(b'{"error": "down"}')
        )

    monkeypatch.setattr(base, "_urlopen", http_error)
    assert base.post_json("https://example.test/x", {}, {}, 5) == (500, {"error": "down"})

    def offline(request, timeout):
        raise urllib.error.URLError("no route")

    monkeypatch.setattr(base, "_urlopen", offline)
    with pytest.raises(LLMError, match=r"couldn't reach example.test"):
        base.post_json("https://example.test/x", {}, {}, 5)


def test_tests_never_reach_the_network() -> None:
    with pytest.raises(AssertionError, match="must not use the network"):
        base.post_json("https://api.anthropic.com/v1/messages", {}, {}, 1)


def test_api_key_is_kept_in_the_key_store(memory_keyring: dict) -> None:
    assert secrets.get_api_key() is None
    secrets.set_api_key("  sk-ant-123456  ")
    assert memory_keyring[(secrets.SERVICE, secrets.USERNAME)] == "sk-ant-123456"
    assert secrets.masked(secrets.get_api_key()) == "saved (...3456)"
    secrets.delete_api_key()
    secrets.delete_api_key()  # nothing to delete: fine
    assert secrets.masked(secrets.get_api_key()) == "not saved"
    with pytest.raises(secrets.SecretError):
        secrets.set_api_key(" ")


def test_make_provider() -> None:
    assert llm.make_provider("off") is None
    with pytest.raises(LLMError, match="No Claude API key"):
        llm.make_provider("claude")
    provider = llm.make_provider("claude", get_key=lambda: "k")
    assert (provider.name, provider.model) == ("Claude API", "claude-opus-5-5")
    local = llm.make_provider("ollama", "", ollama_url="http://127.0.0.1:9999")
    assert (local.url, local.model) == ("http://127.0.0.1:9999", "qwen2.5")
    assert llm.default_model("claude") == claude.MODELS[0]
