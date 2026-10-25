"""PySide6 windows: tray, inbox, history, search, settings, dashboard (Phase 4)."""

import contextlib
import io

# QFluentWidgets prints an advert when it is first imported; keep it out of the terminal.
with contextlib.redirect_stdout(io.StringIO()):
    import qfluentwidgets  # noqa: F401
