"""Logging to a rotating file in the app data folder, plus the console."""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FILENAME = "autoshelf.log"
_FILE_FORMAT = "%(asctime)s %(levelname)-7s %(name)s [%(threadName)s]: %(message)s"
_CONSOLE_FORMAT = "%(asctime)s %(levelname)-7s %(message)s"


def setup_logging(log_dir: Path, *, verbose: bool = False) -> None:
    """Configure the ``autoshelf`` logger. Safe to call more than once."""
    logger = logging.getLogger("autoshelf")
    logger.setLevel(logging.DEBUG)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    file_handler = RotatingFileHandler(
        log_dir / LOG_FILENAME, maxBytes=1_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(_FILE_FORMAT))
    logger.addHandler(file_handler)

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter(_CONSOLE_FORMAT, datefmt="%H:%M:%S"))
    logger.addHandler(console)
