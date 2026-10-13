"""The Claude API (Anthropic) as an LLM provider.

Uses the Messages API: ``POST https://api.anthropic.com/v1/messages`` with the headers
``x-api-key``, ``anthropic-version: 2023-06-01`` and ``content-type: application/json``.
The reply's content is a list of blocks; only ``text`` blocks are kept (newer models also
return ``thinking`` blocks). Errors come back as
``{"type": "error", "error": {"type": ..., "message": ...}}``.
"""

from collections.abc import Sequence

from autoshelf.llm.base import DEFAULT_TIMEOUT_SECONDS, LLMError, Message, Transport, post_json

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
# Newest first; the first is the default. Any other model id can be typed in Settings.
MODELS = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5")
DEFAULT_MODEL = MODELS[0]
# Room for the model's own thinking as well as the answer.
MIN_MAX_TOKENS = 4096


class ClaudeProvider:
    """Sends conversations to the Claude API with the user's API key."""

    name = "Claude API"

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        *,
        transport: Transport = post_json,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not api_key:
            raise LLMError("No Claude API key saved; add one in Settings")
        self.model = model or DEFAULT_MODEL
        self._api_key = api_key
        self._transport = transport
        self._timeout = timeout

    def complete(self, system: str, messages: Sequence[Message], *, max_tokens: int = 2048) -> str:
        """The reply's text. Raises LLMError with the API's message on failure."""
        body = {
            "model": self.model,
            "max_tokens": max(max_tokens, MIN_MAX_TOKENS),
            "system": system,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
        }
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        }
        status, reply = self._transport(API_URL, headers, body, self._timeout)
        if status != 200:
            raise LLMError(f"Claude API error ({status}): {_error_message(reply)}")
        text = "".join(
            block.get("text", "")
            for block in reply.get("content", [])
            if isinstance(block, dict) and block.get("type") == "text"
        ).strip()
        if not text:
            reason = reply.get("stop_reason") or "no text"
            raise LLMError(f"Claude API returned no answer ({reason})")
        return text


def _error_message(reply) -> str:
    if isinstance(reply, dict):
        error = reply.get("error")
        if isinstance(error, dict) and error.get("message"):
            return f"{error.get('type', 'error')}: {error['message']}"
        if reply.get("raw"):
            return str(reply["raw"])
    return "unexpected reply"
