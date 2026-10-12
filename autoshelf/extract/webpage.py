"""HTML pages via BeautifulSoup (also used for EPUB chapters)."""

from typing import BinaryIO

from bs4 import BeautifulSoup

from autoshelf.extract.base import Content, TextBudget, non_empty

MAX_HTML_BYTES = 8 * 1024 * 1024
_NON_CONTENT_TAGS = ["script", "style", "noscript", "template", "svg"]


def html_to_text(markup: bytes | str) -> str:
    """Return the visible text of an HTML document or fragment."""
    return _visible_text(BeautifulSoup(markup, "html.parser"))


def _visible_text(soup: BeautifulSoup) -> str:
    for tag in soup(_NON_CONTENT_TAGS):
        tag.decompose()
    body = soup.body or soup
    return body.get_text("\n")


def extract(file: BinaryIO, max_chars: int) -> Content:
    """Read the page title, meta description/keywords and visible body text."""
    data = file.read(MAX_HTML_BYTES + 1)
    soup = BeautifulSoup(data[:MAX_HTML_BYTES], "html.parser")
    title = soup.title.get_text() if soup.title else None
    metadata = non_empty(description=_meta(soup, "description"), keywords=_meta(soup, "keywords"))
    budget = TextBudget(max_chars)
    budget.add(_visible_text(soup))
    return Content(
        text=budget.text,
        title=title,
        metadata=metadata,
        truncated=budget.truncated or len(data) > MAX_HTML_BYTES,
    )


def _meta(soup: BeautifulSoup, name: str) -> str | None:
    tag = soup.find("meta", attrs={"name": name})
    return tag.get("content") if tag else None
