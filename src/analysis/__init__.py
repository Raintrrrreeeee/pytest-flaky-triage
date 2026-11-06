"""Statistical analysis of structured pytest execution records."""

from .core import analyse
from .records import InputError, load_records

__all__ = ["InputError", "analyse", "load_records"]
