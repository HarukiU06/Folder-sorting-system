"""``autoshelf:`` links, opened by notification buttons (SPEC Phase 14).

A notification about a file waiting in the Inbox offers its best folders as buttons. Each
button opens a link such as ``autoshelf:assign?offer=Xy3...&choice=1``; Windows starts
AutoShelf with it, and that launch hands it to the running app. Any web page can open an
``autoshelf:`` link too, so a link carries no paths: only a random, single-use token for
an offer stored in AutoShelf's database (which file, which folders). An unknown, used or
old token does nothing.
"""

import re
import secrets
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from autoshelf.db import repository

SCHEME = "autoshelf"
INBOX = "inbox"  # open the Inbox (on the offered file, if any)
ASSIGN = "assign"  # put the offered file into one of the offered folders
ACTIONS = frozenset({INBOX, ASSIGN})
OFFER_PREFIX = "offer:"
OFFER_DAYS = 14  # a notification older than this does nothing
MAX_OFFERS = 100  # older offers are forgotten first
MAX_CHOICES = 3
TOKEN = re.compile(r"[A-Za-z0-9_-]{16,64}")


class LinkError(ValueError):
    """A link that can't be followed (malformed, unknown, used or out of date)."""


@dataclass(frozen=True)
class Link:
    """A parsed ``autoshelf:`` link."""

    action: str
    offer: str | None = None
    choice: int | None = None


@dataclass(frozen=True)
class Offer:
    """What a notification offered: a waiting file and the folders on its buttons."""

    token: str
    path: Path
    folders: tuple[str, ...]


def link(action: str, offer: str | None = None, choice: int | None = None) -> str:
    """``autoshelf:assign?offer=...&choice=0`` and the like."""
    query = {}
    if offer is not None:
        query["offer"] = offer
    if choice is not None:
        query["choice"] = str(choice)
    return f"{SCHEME}:{action}" + (f"?{urlencode(query)}" if query else "")


def parse(url: str) -> Link:
    """Read a link strictly. Raises LinkError."""
    parts = urlsplit(url.strip())
    if parts.scheme.casefold() != SCHEME:
        raise LinkError(f"Not an AutoShelf link: {url!r}")
    action = parts.path.strip("/").casefold()
    if action not in ACTIONS:
        raise LinkError(f"Unknown AutoShelf link: {url!r}")
    query = parse_qs(parts.query, strict_parsing=False)
    if any(len(values) != 1 for values in query.values()) or set(query) - {"offer", "choice"}:
        raise LinkError(f"Unknown AutoShelf link: {url!r}")
    offer = query.get("offer", [None])[0]
    if offer is not None and not TOKEN.fullmatch(offer):
        raise LinkError("The link's offer isn't valid")
    choice_text = query.get("choice", [None])[0]
    choice = None
    if choice_text is not None:
        if not choice_text.isdigit() or int(choice_text) >= MAX_CHOICES:
            raise LinkError("The link's choice isn't valid")
        choice = int(choice_text)
    if action == ASSIGN and (offer is None or choice is None):
        raise LinkError("The link doesn't say which folder")
    return Link(action, offer, choice)


# --- offers ------------------------------------------------------------------------------------


def make_offer(
    conn: sqlite3.Connection, path: Path, folders: Sequence[str], *, now: datetime | None = None
) -> str:
    """Remember what a notification offers; returns the token its links carry."""
    now = now or datetime.now(UTC)
    token = secrets.token_urlsafe(18)
    _forget_old(conn, now)
    repository.set_setting(
        conn,
        OFFER_PREFIX + token,
        {"path": str(path), "folders": list(folders)[:MAX_CHOICES], "made": now.isoformat()},
    )
    return token


def get_offer(conn: sqlite3.Connection, token: str, *, now: datetime | None = None) -> Offer:
    """The offer behind a token. Raises LinkError if it is unknown, used or out of date."""
    now = now or datetime.now(UTC)
    stored = repository.get_setting(conn, OFFER_PREFIX + token) if TOKEN.fullmatch(token) else None
    if not isinstance(stored, dict):
        raise LinkError("This notification has already been answered, or is out of date.")
    made = datetime.fromisoformat(stored["made"])
    if now - made > timedelta(days=OFFER_DAYS):
        use_offer(conn, token)
        raise LinkError("This notification is out of date.")
    return Offer(token, Path(stored["path"]), tuple(stored["folders"]))


def use_offer(conn: sqlite3.Connection, token: str) -> None:
    """Forget an offer once it has been answered (its links do nothing from then on)."""
    conn.execute("DELETE FROM settings WHERE key = ?", (OFFER_PREFIX + token,))


def _forget_old(conn: sqlite3.Connection, now: datetime) -> None:
    rows = conn.execute(
        "SELECT key, json_extract(value, '$.made') AS made FROM settings WHERE key LIKE ?"
        " ORDER BY made DESC",
        (OFFER_PREFIX + "%",),
    ).fetchall()
    cutoff = (now - timedelta(days=OFFER_DAYS)).isoformat()
    for index, row in enumerate(rows):
        if index >= MAX_OFFERS - 1 or (row["made"] or "") < cutoff:
            conn.execute("DELETE FROM settings WHERE key = ?", (row["key"],))
