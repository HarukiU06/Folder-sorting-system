"""The app's look: Windows 11 style (Fluent) widgets in Windows' light or dark mode."""

from qfluentwidgets import Theme, setTheme, setThemeColor

ACCENT = "#2f6f8f"  # the colour of AutoShelf's icon


def apply_theme() -> None:
    """Follow Windows' light/dark setting, with AutoShelf's accent colour."""
    setTheme(Theme.AUTO)
    setThemeColor(ACCENT)
