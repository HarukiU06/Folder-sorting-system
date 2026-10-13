"""A local Ollama server as an LLM provider: nothing leaves the computer.

Uses ``POST {url}/api/chat`` with ``{"model", "messages", "stream": false}``; the reply is
``{"message": {"role": "assistant", "content": ...}}``, errors are ``{"error": ...}``.
"""

from collections.abc import Sequence

from autoshelf.llm.base import DEFAULT_TIMEOUT_SECONDS, LLMError, Message, Transport, post_json

DEFAULT_URL = "http://localhost:11434"
DEFAULT_MODEL = "qwen2.5"  # multilingual (English and Japanese); any pulled model works


class OllamaProvider:
    """Sends conversations to an Ollama server (by default on this PC)."""

    name = "Ollama"

    def __init__(
        self,
        url: str = DEFAULT_URL,
        model: str = DEFAULT_MODEL,
        *,
        transport: Transport = post_json,
        timeout: float = DEFAULT_TIMEOUT_SECONDS * 3,  # local models can be slow
    ) -> None:
        self.url = (url or DEFAULT_URL).rstrip("/")
        self.model = model or DEFAULT_MODEL
        self._transport = transport
        self._timeout = timeout

    def complete(self, system: str, messages: Sequence[Message], *, max_tokens: int = 2048) -> str:
        """The reply's text. Raises LLMError (e.g. Ollama not running, model not pulled)."""
        body = {
            "model": self.model,
            "stream": False,
            "options": {"num_predict": max_tokens},
            "messages": [
                {"role": "system", "content": system},
                *({"role": m.role, "content": m.content} for m in messages),
            ],
        }
        status, reply = self._transport(
            f"{self.url}/api/chat", {"content-type": "application/json"}, body, self._timeout
        )
        if status != 200 or not isinstance(reply, dict):
            message = reply.get("error") if isinstance(reply, dict) else None
            raise LLMError(f"Ollama error ({status}): {message or 'unexpected reply'}")
        text = (reply.get("message") or {}).get("content", "").strip()
        if not text:
            raise LLMError("Ollama returned no answer")
        return text
