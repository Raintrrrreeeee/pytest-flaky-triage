"""Command-line entry point for pytest-flaky-triage."""

import os
from pathlib import Path
import shutil
import subprocess
import sys

from flaky_triage import runner


def _use_pure_python_runner():
    if os.name == "nt":
        return True
    if os.environ.get("FLAKY_PURE_PYTHON") == "1":
        return True
    if shutil.which("bash") is None:
        return True
    return False


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    pkg_dir = Path(__file__).resolve().parent
    repo_root = pkg_dir.parents[1]
    os.environ.setdefault("FLAKY_TOOL_PYTHON", sys.executable)
    os.environ.setdefault("FLAKY_CLI_NAME", "pytest-flaky-triage")

    if not (repo_root / "scripts" / "detect.sh").is_file():
        os.environ["FLAKY_INSTALLED_PACKAGE"] = "1"

    if _use_pure_python_runner():
        if args and args[0] == "--demo":
            return runner.run_demo(args[1:])
        return runner.run_detect(args)

    env = os.environ.copy()
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
