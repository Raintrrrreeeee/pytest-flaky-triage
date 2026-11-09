"""scripts/demo.sh runs the scenario end to end and lines it up with the answer key."""
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "scripts" / "demo.sh"
OUT = ROOT / "out" / "selftest" / "demo"
CATEGORIES = {"hash_order", "randomness", "race_condition", "shared_state", "timezone", "deterministic"}


def test_demo_runs_and_every_scenario_test_is_in_the_answer_key():
    if os.name == "nt" or os.environ.get("FLAKY_PURE_PYTHON") == "1":
        env = os.environ.copy()
        env["FLAKY_PURE_PYTHON"] = "1"
        env["PYTHONPATH"] = os.pathsep.join(
            [str(ROOT / "src"), *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])]
        )
        cmd = [sys.executable, "-m", "flaky_triage.cli", "--demo"]
    else:
        env = None
        cmd = ["bash", str(DEMO)]
    result = subprocess.run(
        [*cmd, "--out", str(OUT),
         "--repeats", "2", "--hash-seeds", "1", "--orders", "1",
         "--timezones", "UTC Pacific/Kiritimati", "--isolated", "0"],
        env=env,
        capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, result.stderr

    rows = re.findall(r"^  (test_\w+)\s+(\S+)\s+\d+/\d+", result.stdout, re.MULTILINE)
    collected = (OUT / "collected.txt").read_text().split()
    assert sorted(name for name, _ in rows) == sorted(c.split("::")[-1] for c in collected)
    assert {category for _, category in rows} <= CATEGORIES
    assert "out/selftest/demo/results.csv" in result.stdout
