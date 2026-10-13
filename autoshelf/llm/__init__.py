"""Optional LLM providers (off by default): the Claude API or a local Ollama server."""

from collections.abc import Callable

from autoshelf.llm import claude, ollama
from autoshelf.llm.base import LLMError, Message, Provider

OFF = "off"
CLAUDE = "claude"
OLLAMA = "ollama"
PROVIDER_NAMES = {OFF: "Off", CLAUDE: "Claude API (Anthropic)", OLLAMA: "Ollama (on this PC)"}
PRIVACY_NOTE = (
    "Off by default. When on, AutoShelf sends text from the files you ask about (a summary, "
    "a better name, a question about your library, flashcards) to the provider. With the "
    "Claude API that text goes to Anthropic's servers; with Ollama it stays on this PC. "
    "Sorting itself never uses it."
)

__all__ = ["CLAUDE", "OFF", "OLLAMA", "LLMError", "Message", "Provider", "make_provider"]


def default_model(provider: str) -> str:
    """The model used when none is set."""
    return claude.DEFAULT_MODEL if provider == CLAUDE else ollama.DEFAULT_MODEL


def make_provider(
    provider: str,
    model: str = "",
    *,
    ollama_url: str = ollama.DEFAULT_URL,
    get_key: Callable[[], str | None] | None = None,
) -> Provider | None:
    """The configured provider, or None when it is off. Raises LLMError if incomplete."""
    if provider == CLAUDE:
        from autoshelf.llm.secrets import get_api_key

        key = (get_key or get_api_key)()
        return claude.ClaudeProvider(key or "", model or claude.DEFAULT_MODEL)
    if provider == OLLAMA:
        return ollama.OllamaProvider(ollama_url, model or ollama.DEFAULT_MODEL)
    return None
