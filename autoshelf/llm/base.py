"""The optional LLM provider interface (SPEC section 2). Off by default.

A provider turns a system prompt and a short conversation into text. Two exist: the
Claude API (``llm.claude``) and a local Ollama server (``llm.ollama``). Both talk plain
HTTPS/HTTP through ``post_json``, so there is no extra dependency, and tests replace the
transport. File text is only ever sent when the user turned a provider on and asked for
something (a summary, a name, an answer).
"""

import json
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

DEFAULT_TIMEOUT_SECONDS = 120.0


class LLMError(Exception):
    """The provider couldn't answer (not configured, unreachable, or it refused)."""


@dataclass(frozen=True)
class Message:
    """One turn of a conversation: role "user" or "assistant"."""

    role: str
    content: str


class Provider(Protocol):
    """Something that completes a conversation."""

    name: str  # shown to the user, e.g. "Claude API"
    model: str

    def complete(self, system: str, messages: Sequence[Message], *, max_tokens: int = 2048) -> str:
        """The assistant's reply as text. Raises LLMError."""
        ...


# (url, headers, JSON body, timeout) -> (HTTP status, decoded JSON response)
Transport = Callable[[str, dict[str, str], dict[str, Any] | None, float], tuple[int, Any]]


def _urlopen(request: urllib.request.Request, timeout: float):
    return urllib.request.urlopen(request, timeout=timeout)


def post_json(
    url: str, headers: dict[str, str], body: dict[str, Any] | None, timeout: float
) -> tuple[int, Any]:
    """POST ``body`` as JSON (GET when it is None) and decode the JSON reply.

    HTTP errors are returned with their status, not raised, so callers can show the
    provider's own error message. Network failures raise LLMError.
    """
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers=headers, method="GET" if body is None else "POST"
    )
    try:
        with _urlopen(request, timeout) as response:
            return response.status, _decode(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _decode(exc.read())
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise LLMError(f"couldn't reach {url.split('/')[2]}: {reason}") from exc


def _decode(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"raw": raw[:500].decode("utf-8", "replace")}


def clip(text: str, limit: int) -> str:
    """At most ``limit`` characters of text (what is sent is kept small)."""
    return text if len(text) <= limit else text[:limit] + "\n[...]"
