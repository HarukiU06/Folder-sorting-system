"""Text and metadata extractors, one module per file family, chosen by extension."""

from autoshelf.extract.registry import FILENAME_ONLY, Extraction, extract

__all__ = ["FILENAME_ONLY", "Extraction", "extract"]
