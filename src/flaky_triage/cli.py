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


def _resolve_out_hint(args):
    out_hint = "out"
    for idx, tok in enumerate(args):
        if tok == "--out" and idx + 1 < len(args):
            out_hint = args[idx + 1]
        elif tok.startswith("--out="):
            out_hint = tok.split("=", 1)[1]
    return out_hint


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    pkg_dir = Path(__file__).resolve().parent
    repo_root = pkg_dir.parents[1]
    os.environ.setdefault("FLAKY_TOOL_PYTHON", sys.executable)
    os.environ.setdefault("FLAKY_CLI_NAME", "pytest-flaky-triage")

    is_repo_checkout = (repo_root / "scripts" / "detect.sh").is_file()
    if not is_repo_checkout:
        os.environ["FLAKY_INSTALLED_PACKAGE"] = "1"

    try:
        if _use_pure_python_runner():
            if args and args[0] == "--demo":
                return runner.run_demo(args[1:])
            return runner.run_detect(args)

        env = os.environ.copy()
        script_dir = pkg_dir / "runtime"
        if not is_repo_checkout:
            env["FLAKY_INSTALLED_PACKAGE"] = "1"

        if args and args[0] == "--demo":
            script = script_dir / "demo.sh"
            cmd = ["bash", str(script), *args[1:]]
        else:
            script = script_dir / "detect.sh"
            cmd = ["bash", str(script), *args]

        proc = subprocess.run(cmd, env=env, check=False)
        if proc.returncode < 0:
            return 128 + (-proc.returncode)
        return proc.returncode
    except KeyboardInterrupt:
        out_hint = _resolve_out_hint(args)
        sys.stderr.write(
            f"\n[detect] Interrupted by user (Ctrl+C). Partial logs and results (if any) are in {out_hint}. "
            "To resume or inspect existing data without re-running pytest, use --report-only.\n"
        )
        sys.stderr.flush()
        return 130


if __name__ == "__main__":
    sys.exit(main())
