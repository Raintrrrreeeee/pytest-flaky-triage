"""Command-line entry point for pytest-flaky-triage."""

import os
from pathlib import Path
import subprocess
import sys


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    pkg_dir = Path(__file__).resolve().parent
    repo_root = pkg_dir.parents[1]
    env = os.environ.copy()
    env.setdefault("FLAKY_TOOL_PYTHON", sys.executable)
    env.setdefault("FLAKY_CLI_NAME", "pytest-flaky-triage")

    if (repo_root / "scripts" / "detect.sh").is_file():
        script_dir = repo_root / "scripts"
    else:
        env["FLAKY_INSTALLED_PACKAGE"] = "1"
        script_dir = pkg_dir / "runtime"

    if args and args[0] == "--demo":
        script = script_dir / "demo.sh"
        cmd = ["bash", str(script), *args[1:]]
    else:
        script = script_dir / "detect.sh"
        cmd = ["bash", str(script), *args]

    proc = subprocess.run(cmd, env=env, check=False)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
