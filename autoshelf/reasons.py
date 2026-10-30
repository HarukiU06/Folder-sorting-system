"""Why AutoShelf did something, as data: a reason code and what it refers to.

The core (classifier, pipeline, rules, learning, organize, rename) produces reasons like
``{"code": "too_close", "folder": "Finance", "score": 0.52, "rival": "Travel", ...}`` and
stores them with history rows; it never writes sentences there. ``wording`` builds the
sentence when a reason is shown, so the app and the command line say the same thing and
the words can be translated. History rows written before reasons existed only have an
English ``"explanation"`` text, which is still shown as it is.
"""

from typing import Any

Reason = dict[str, Any]

# Decisions of the classifier (classifier.choose).
CLEAR_MATCH = "clear_match"  # folder, score, rival, rival_score: moved automatically
LOW_SCORE = "low_score"  # folder, score, threshold: the best folder scored too low
TOO_CLOSE = "too_close"  # folder, score, rival, rival_score: two folders fit about equally
NO_FOLDERS = "no_folders"  # there is no folder to sort into
NO_CLASSIFIER = "no_classifier"  # sorting by content isn't available (no language model)

# Decisions of the pipeline that override the classifier.
RULE = "rule"  # rule, matched: a list of what its conditions found
RULE_FOLDER_MISSING = "rule_folder_missing"  # rule, folder: the rule's folder is gone
FOLDER_GONE = "folder_gone"  # folder: the chosen folder vanished before the move

# The user's own actions.
USER_MOVED = "user_moved"  # from_folder, to_folder: a correction made in History
RENAMED = "renamed"  # old_name: a rename the user approved

# Sorted again when a new folder appeared (Library editing).
RESORTED = "resorted"  # folder, trigger (the new folder), match ("name", "rule" or "content")

# Files AutoShelf made (Phase 13).
CREATED = "created"  # operation, sources: e.g. a compressed copy


def reason(code: str, **details: Any) -> Reason:
    """A reason as stored in history: ``{"code": code, **details}`` (JSON-ready)."""
    return {"code": code, **details}
