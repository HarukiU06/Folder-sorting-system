"""History upkeep: settle actions that a crash left half-recorded."""

import logging
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from autoshelf.actions.mover import status_after_move
from autoshelf.db import repository
from autoshelf.fsutil import sha256_file

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Reconciled:
    """How one history row was settled."""

    history_id: int
    state: str
    note: str


def reconcile_pending(conn: sqlite3.Connection) -> list[Reconciled]:
    """Settle every ``pending`` history row by checking where the file actually is.

    Run this at startup, before the watcher moves anything, and only from the process
    that owns the watcher (a pending row may belong to a move that is in progress).
    Only database rows change here; no file is touched.
    """
    settled: list[Reconciled] = []
    for entry in repository.list_history_in_state(conn, "pending"):
        state, note = _settle(entry)
        repository.set_history_state(conn, entry.id, state, note=note)
        if state == "done" and entry.file_id is not None:
            dst, root = Path(entry.dst), Path(entry.root)
            repository.set_file_location(
                conn, entry.file_id, dst, status_after_move(dst, root, entry.action)
            )
        settled.append(Reconciled(entry.id, state, note))

    for target_id in repository.undos_with_stale_target(conn):
        note = "an undo of this entry had completed before a restart"
        repository.set_history_state(conn, target_id, "undone", note=note)
        settled.append(Reconciled(target_id, "undone", note))

    for item in settled:
        log.info("Reconciled history #%d as %s: %s", item.history_id, item.state, item.note)
    return settled


def _settle(entry: repository.HistoryEntry) -> tuple[str, str]:
    src_exists = os.path.lexists(entry.src)
    dst_exists = os.path.lexists(entry.dst)
    if dst_exists and not src_exists:
        if entry.sha256 and _hash_or_none(Path(entry.dst)) != entry.sha256:
            return "failed", "a different file is at the destination and the original is missing"
        return "done", "the move had completed before a restart"
    if src_exists and not dst_exists:
        return "failed", "the move had not happened before a restart"
    if src_exists and dst_exists:
        return "failed", "the move had not happened; another file took the destination name"
    return "failed", "the file is missing from both its source and destination"


def _hash_or_none(path: Path) -> str | None:
    try:
        return sha256_file(path)
    except OSError:
        return None
