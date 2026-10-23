"""The "Why here?" panel: what AutoShelf based a decision on (SPEC section 4.7)."""

import html
from collections.abc import Mapping
from pathlib import Path

from PySide6.QtWidgets import QDialog, QDialogButtonBox, QTextBrowser, QVBoxLayout

from autoshelf import reasons, wording

BAR_PIXELS = 120
EMPTY = "Select a file to see why it went where it went."


def _bar(score: float, colour: str = "#3b82a0") -> str:
    filled = max(1, min(BAR_PIXELS, round(score * BAR_PIXELS)))
    return (
        '<table cellspacing="0" cellpadding="0"><tr>'
        f'<td bgcolor="{colour}" width="{filled}" height="8"></td>'
        f'<td width="{BAR_PIXELS - filled}"></td></tr></table>'
    )


def _relative(path: str, root: Path | None) -> str:
    if root is not None:
        try:
            return Path(path).relative_to(root).as_posix()
        except ValueError:
            pass
    return Path(path).name


def why_html(details: Mapping | None, root: Path | None = None, headline: str = "") -> str:
    """Render stored decision details as HTML for the panel."""
    if not details:
        body = f"<p>{html.escape(headline)}</p>" if headline else ""
        return body + "<p><i>No explanation was stored for this decision.</i></p>"
    parts = []
    if headline:
        parts.append(f"<p><b>{html.escape(headline)}</b></p>")
    rule = details.get("rule")
    if rule:
        if "conditions" in rule:
            when = " and ".join(wording.condition(item) for item in rule["conditions"])
            found = wording.rule_matches(rule.get("matched") or [])
        else:  # written before rules were stored as data
            when, found = rule["condition"], rule.get("match", "")
        parts.append(
            f"<p>Rule <b>{html.escape(rule['name'])}</b>: {html.escape(when)} "
            f"&rarr; {html.escape(rule['target'])}<br>"
            f"<small>{html.escape(found)}</small></p>"
        )
    code = (details.get("reason") or {}).get("code", reasons.RULE)
    why = wording.explanation(details)
    if why and not (rule and code == reasons.RULE):
        parts.append(f"<p>{html.escape(why)}</p>")
    candidates = details.get("candidates") or []
    if candidates:
        rows = [
            "<tr><th align='left'>Folder</th><th align='left'>Score</th><th></th>"
            "<th align='left'>Name &amp; description</th><th align='left'>Similar files</th>"
            "<th align='left'>Learned</th></tr>"
        ]
        for item in candidates:
            chosen = item["folder"] == details.get("choice")
            folder = html.escape(item["folder"])
            rows.append(
                "<tr>"
                f"<td>{'<b>' + folder + '</b>' if chosen else folder}</td>"
                f"<td>{item['score']:.2f}</td><td>{_bar(max(item['score'], 0.0))}</td>"
                f"<td>{item.get('name', 0.0):.2f}</td>"
                f"<td>{'' if 'files' not in item else format(item['files'], '.2f')}</td>"
                f"<td>{format(item['learned'], '+.2f') if item.get('learned') else ''}</td>"
                "</tr>"
            )
        parts.append(
            "<p><b>Best folders</b></p><table cellspacing='4'>" + "".join(rows) + "</table>"
        )
    focus = html.escape(str(details.get("focus") or ""))
    similar = details.get("similar_files") or []
    if similar:
        items = "".join(
            f"<li>{html.escape(_relative(item['path'], root))} "
            f"<small>({item['similarity']:.2f})</small></li>"
            for item in similar
        )
        parts.append(f"<p><b>Most similar files in {focus}</b></p><ul>{items}</ul>")
    keywords = details.get("keywords") or []
    if keywords:
        words = ", ".join(html.escape(word) for word in keywords)
        parts.append(f"<p><b>Words in common with {focus}:</b> {words}</p>")
    for adjustment in details.get("learned") or []:
        examples = "; ".join(
            f"{html.escape(item['file'] or 'a file')} &rarr; {html.escape(item['to'])}"
            for item in adjustment["examples"]
        )
        parts.append(
            f"<p><b>Learned {adjustment['amount']:+.2f}</b> for "
            f"{html.escape(adjustment['folder'])}: you moved similar files away from it "
            f"<small>({examples})</small></p>"
        )
    if details.get("corrects"):
        parts.append(f"<p>You moved it here, correcting #{details['corrects']}.</p>")
    return "".join(parts)


class WhyDialog(QDialog):
    """ "Why is this here?" for one file (from Explorer's right-click menu)."""

    def __init__(self, result, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Why is {result.path.name} here?")
        self.resize(560, 360)
        entry = result.entry
        headline = wording.why_here(result.path, entry, result.here)
        self.panel = WhyPanel()
        root = Path(entry.root) if entry is not None and entry.root else None
        # A move's details explain it; for a file AutoShelf made, the headline says it all.
        explained = entry is not None and result.here and entry.action in ("move", "rename")
        details = entry.details if explained else None
        self.panel.setHtml(
            why_html(details, root, headline) if details else f"<p>{html.escape(headline)}</p>"
        )
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(self.panel, 1)
        layout.addWidget(buttons)


class WhyPanel(QTextBrowser):
    """Shows the stored "why" of one decision."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setOpenLinks(False)
        self.setPlaceholderText(EMPTY)

    def show_details(
        self, details: Mapping | None, root: Path | None = None, headline: str = ""
    ) -> None:
        """Display the details of one decision."""
        self.setHtml(why_html(details, root, headline))

    def clear_details(self) -> None:
        """Show nothing (no selection)."""
        self.clear()
