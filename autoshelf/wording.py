"""Words for what the core reports as data, shared by the app and the command line.

The core (``actions``, ``ml``, ``features``) returns structured results; the sentences
users read are built here, when they are shown, so the app and the command line say the
same thing and the wording lives in one place.
"""

from autoshelf.ml.calibrate import MAX_WRONG, MIN_FILES, MIN_FOLDERS, Calibration, LevelResult


def percent(share: float) -> str:
    """0.873 -> "87%"."""
    return f"{share:.0%}"


def level_result(result: LevelResult) -> str:
    """One line of a calibration: "At Balanced: 87% correct, 9% to the Inbox, 4% wrong"."""
    return (
        f"At {result.level.name}: {percent(result.correct_share)} correct, "
        f"{percent(result.inbox_share)} to the Inbox, {percent(result.wrong_share)} wrong"
    )


def calibration(result: Calibration, current: int | None = None) -> list[str]:
    """What a calibration found, line by line. ``current``: the level in use, if a preset."""
    if not result.enough:
        return [
            f"Not enough files to tell yet: AutoShelf needs at least {MIN_FILES} files in "
            f"{MIN_FOLDERS} or more folders ({result.files} found). Try again later."
        ]
    lines = [
        f"Each of the {result.files} files in your {result.folders} folders was sorted as if "
        "it had just arrived:"
    ]
    for index, item in enumerate(result.results):
        lines.append(level_result(item) + ("  (now)" if index == current else ""))
    if result.recommended is not None:
        level = result.results[result.recommended].level
        lines.append(
            f"Recommended: {level.name}, the most automatic setting that puts at most "
            f"{percent(MAX_WRONG)} of the files in a wrong folder."
        )
    return lines
