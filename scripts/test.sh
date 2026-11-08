#!/usr/bin/env bash
# test.sh - run the tool's own tests (runner, parser, analysis, report).
# Extra arguments are passed to pytest, e.g. scripts/test.sh -k parser
set -euo pipefail

TOOL_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
if [[ -x $TOOL_ROOT/.venv/bin/python ]]; then
    PYTHON=$TOOL_ROOT/.venv/bin/python
else
    PYTHON=$(command -v python3 || command -v python || true)
fi

if [[ -z $PYTHON || ! -x $PYTHON ]] || ! "$PYTHON" -m pytest --version >/dev/null 2>&1; then
    echo "test.sh: error: pytest environment not found; set it up first:" >&2
    echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 3
fi

cd "$TOOL_ROOT"
# tests/fixtures holds small projects that detect.sh runs; they are inputs to
# the tests, not tests of the tool.
exec "$PYTHON" -m pytest tests -p no:randomly -p no:cacheprovider \
    --ignore=tests/fixtures "$@"
