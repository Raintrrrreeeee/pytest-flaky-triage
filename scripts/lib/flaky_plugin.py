"""Thin wrapper delegating to src/flaky_triage/runtime/lib/flaky_plugin.py."""

from pathlib import Path
import runpy
import sys

_RUNTIME_PLUGIN = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "flaky_triage"
    / "runtime"
    / "lib"
    / "flaky_plugin.py"
)

if __name__ == "__main__":
    sys.exit(runpy.run_path(str(_RUNTIME_PLUGIN), run_name="__main__"))
else:
    globals().update(runpy.run_path(str(_RUNTIME_PLUGIN)))
