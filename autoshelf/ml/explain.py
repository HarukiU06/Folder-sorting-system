"""Why a file went where it went (SPEC section 4.7), without any LLM.

For every decision AutoShelf stores, next to the top three folders and their scores:

- the most similar files already in the chosen (or best) folder,
- the words the file shares with that folder, ranked by TF-IDF,
- any penalty learned from the user's corrections,
- or the rule that fired.

Everything is stored as data; ``wording.decision_lines`` turns it into readable lines
(CLI and app) when it is shown.
"""

import math
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence

from autoshelf.ml.classifier import Classification, FolderScore
from autoshelf.ml.learned import Adjustment
from autoshelf.reasons import RULE, reason

TOP_KEYWORDS = 6
SIMILAR_FILES = 3

_LATIN_WORD = re.compile(r"[^\W\d_](?:[\w'-]*\w)?")
# Kana, kanji and half-width kana.
_CJK_RUN = re.compile("[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff66-\uff9f]+")
# Hiragana is mostly grammar ("して", "から"); topic words are kanji and katakana.
_HIRAGANA = re.compile("[\u3040-\u309f]+")
# Very common English words that say nothing about a topic.
_STOPWORDS = frozenset(
    """
    a about above after again against all also am an and any are as at be because been before
    being below between both but by can could did do does doing down during each few for from
    further had has have having he her here hers him his how i if in into is it its itself just
    me more most my no nor not now of off on once only or other our ours out over own same she
    should so some such than that the their theirs them then there these they this those through
    to too under until up very was we were what when where which while who whom why will with
    would you your yours page pages file files document new one two use used using via per
    pdf docx doc txt md xlsx pptx copy final draft version
    """.split()
)


def terms(text: str) -> list[str]:
    """Words of a text for keyword matching.

    Latin-script words (two letters or more, lowercased, no stop words) and, because
    Japanese has no spaces, every pair of adjacent kanji or katakana characters (hiragana
    is mostly grammar).
    """
    text = unicodedata.normalize("NFKC", text).casefold()
    found: list[str] = []
    for match in _CJK_RUN.finditer(text):
        run = match.group()
        pairs = (run[i : i + 2] for i in range(len(run) - 1))
        found.extend(pair for pair in pairs if not _HIRAGANA.search(pair))
    for word in _LATIN_WORD.findall(_CJK_RUN.sub(" ", text)):
        word = word.strip("'-")
        if len(word) >= 2 and word not in _STOPWORDS:
            found.append(word)
    return found


def shared_keywords(
    file_text: str, folder_text: str, corpus: Sequence[str], top: int = TOP_KEYWORDS
) -> list[str]:
    """Words the file and the folder have in common, most telling first (TF-IDF).

    ``corpus`` holds the texts of all folders; words found in many of them count less.
    """
    file_counts = Counter(terms(file_text))
    folder_counts = Counter(terms(folder_text))
    shared = file_counts.keys() & folder_counts.keys()
    if not shared:
        return []
    documents = [set(terms(text)) for text in corpus]
    total = len(documents)

    def idf(term: str) -> float:
        containing = sum(1 for words in documents if term in words)
        return math.log((total + 1) / (containing + 1)) + 1.0

    file_size = sum(file_counts.values())
    folder_size = sum(folder_counts.values())
    scored = {
        term: (file_counts[term] / file_size) * (folder_counts[term] / folder_size) * idf(term) ** 2
        for term in shared
    }
    ranked = sorted(scored, key=lambda term: (-scored[term], term))
    return _merge_pairs(ranked, file_text, folder_text)[:top]


def _merge_pairs(keywords: list[str], *texts: str) -> list[str]:
    """Join overlapping character pairs back into words ("所得" + "得税" -> "所得税").

    Only when the longer word occurs in every text, so nothing is invented.
    """
    folded = [unicodedata.normalize("NFKC", text).casefold() for text in texts]
    merged = list(keywords)
    while (pair := _mergeable(merged, folded)) is not None:
        i, j, word = pair
        merged[i] = word
        del merged[j]
    return merged


def _mergeable(keywords: list[str], texts: list[str]) -> tuple[int, int, str] | None:
    for i, first in enumerate(keywords):
        for j, second in enumerate(keywords):
            if i == j or not (_CJK_RUN.fullmatch(first) and _CJK_RUN.fullmatch(second)):
                continue
            word = _join(first, second)
            if word is not None and all(word in text for text in texts):
                return i, j, word
    return None


def _join(first: str, second: str) -> str | None:
    """Join two words that overlap: "所得" and "得税" give "所得税"."""
    for size in range(min(len(first), len(second)) - 1, 0, -1):
        if first[-size:] == second[:size]:
            return first + second[size:]
    return None


def decision_details(
    classification: Classification,
    *,
    keywords: Sequence[str] = (),
    learned: Mapping[str, Adjustment] | None = None,
    folder: str | None = None,
) -> dict:
    """Everything stored with a decision, as JSON-ready data.

    ``folder`` is the folder the similar files and keywords refer to: the chosen one, or
    else the best suggestion.
    """
    details = classification.to_details()
    focus = _focus(classification, folder)
    if focus is not None:
        details["focus"] = focus.folder
        details["similar_files"] = [
            {"path": str(neighbor.path), "similarity": round(neighbor.similarity, 4)}
            for neighbor in focus.neighbors[:SIMILAR_FILES]
        ]
        details["keywords"] = list(keywords)
    shown = {item.folder.casefold() for item in classification.top()}
    details["learned"] = [
        {
            "folder": adjustment.folder,
            "amount": round(adjustment.amount, 4),
            "examples": [
                {"file": item.name, "to": item.to_folder, "similarity": item.similarity}
                for item in adjustment.evidence[:3]
            ],
        }
        for adjustment in (learned or {}).values()
        if adjustment.folder.casefold() in shown
    ]
    return details


def _focus(classification: Classification, folder: str | None) -> FolderScore | None:
    if folder is not None:
        return classification.score_of(folder)
    if classification.choice is not None:
        return classification.choice
    return classification.scores[0] if classification.scores else None


def rule_details(
    name: str, conditions: Sequence[Mapping], target: str, matched: Sequence[Mapping]
) -> dict:
    """What is stored when a rule decided: the rule as it was, and what it found."""
    return {
        "rule": {
            "name": name,
            "conditions": [dict(item) for item in conditions],
            "target": target,
            "matched": [dict(item) for item in matched],
        },
        "reason": reason(RULE, rule=name, matched=[dict(item) for item in matched]),
    }


def keyword_text(name: str, title: str | None, text: str) -> str:
    """The text of a file used for keywords: its name words, title and body."""
    return "\n".join(part for part in (name, title or "", text) if part)


def join_texts(parts: Iterable[str]) -> str:
    """Join non-empty texts with newlines."""
    return "\n".join(part for part in parts if part)
