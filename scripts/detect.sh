#!/usr/bin/env bash
# detect.sh - thin wrapper delegating to src/flaky_triage/runtime/detect.sh.
set -euo pipefail

SCRIPT_DIR=$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
TOOL_ROOT=$(cd -P "$SCRIPT_DIR/.." && pwd -P)
export FLAKY_TOOL_ROOT="${FLAKY_TOOL_ROOT:-$TOOL_ROOT}"
exec "$TOOL_ROOT/src/flaky_triage/runtime/detect.sh" "$@"
