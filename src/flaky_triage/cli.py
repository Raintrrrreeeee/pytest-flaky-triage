"""Command-line entry point for pytest-flaky-triage."""

from pathlib import Path
import subprocess
import sys


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    root = Path(__file__).resolve().parents[2]
    if args and args[0] == "--demo":
        script = root / "scripts" / "demo.sh"
        cmd = ["bash", str(script), *args[1:]]
    else:
        script = root / "scripts" / "detect.sh"
        cmd = ["bash", str(script), *args]
    proc = subprocess.run(cmd, check=False)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
