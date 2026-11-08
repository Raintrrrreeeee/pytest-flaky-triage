"""Allow `python -m flaky_triage` in addition to `pytest-flaky-triage`."""

import sys

from flaky_triage.cli import main

if __name__ == "__main__":
    sys.exit(main())
