"""Make the repository's src-layout packages importable during local tests."""

import sys
from pathlib import Path


SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)
