"""The optional LLM providers: exact requests, replies and errors (no network is used)."""

import email.message
import io
import json
import urllib.error
import urllib.request
import urllib.response

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


def test_redirects_are_not_followed_so_the_key_stays_with_the_api() -> None:
    requested = []

    class Redirecting(urllib.request.BaseHandler):
        handler_order = 100  # answers before the real HTTPS handler

        def https_open(self, request):
            requested.append(request.full_url)
            headers = email.message.Message()
            headers["Location"] = "https://elsewhere.test/collect"
            response = urllib.response.addinfourl(io.BytesIO(b"{}"), headers, request.full_url, 302)
            response.msg = "Found"
            return response

    assert any(isinstance(handler, base._NoRedirect) for handler in base._opener.handlers)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), base._NoRedirect, Redirecting
    )
    request = urllib.request.Request(
        claude.API_URL, data=b"{}", headers={"x-api-key": "sk-ant-secret"}, method="POST"
    )
    with pytest.raises(urllib.error.HTTPError) as caught:
        opener.open(request, timeout=1)
    assert caught.value.code == 302
    assert requested == [claude.API_URL]


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


def test_bulk_tasks_default_to_the_cheapest_claude_model() -> None:
    assert llm.bulk_model("claude", "", "") == "claude-haiku-5-5"
    assert llm.bulk_model("claude", "claude-sonnet-5-5", "") == "claude-haiku-5-5"
    assert llm.bulk_model("claude", "", "claude-opus-5-5") == "claude-opus-5-5"  # chosen
    assert llm.bulk_model("ollama", "llama3.2", "") == "llama3.2"  # one local model
    assert llm.bulk_model("ollama", "", "") == "qwen2.5"
    assert set(claude.MODELS) >= {"claude-opus-5-5", "claude-sonnet-5-5", claude.BULK_MODEL}


def test_the_app_uses_the_bulk_model_for_summaries_names_and_cards(
    paths, memory_keyring: dict
) -> None:
    from dataclasses import replace

    from autoshelf.app_service import AppService

    service = AppService(paths)
    service.save_api_key("sk-ant-test")
    service.save_settings(replace(service.settings(), llm_provider="claude"))
    assert service.provider().model == "claude-opus-5-5"  # questions, reading lists
    assert service.provider(bulk=True).model == "claude-haiku-5-5"
    service.save_settings(replace(service.settings(), llm_bulk_model="claude-sonnet-5-5"))
    assert service.provider(bulk=True).model == "claude-sonnet-5-5"
