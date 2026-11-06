"""Parser fixture tests for scripts/detect.sh and scripts/lib/parse_log.awk.

Each folder in tests/fixtures/parser/ is a small real pytest project for one
unusual case. detect.sh runs it with the pinned pytest, and the results.csv
it writes is compared row by row with the hand-written expected.csv.
"""
import csv
import subprocess
from collections import defaultdict
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DETECT = ROOT / "scripts" / "detect.sh"
FIXTURES = ROOT / "tests" / "fixtures" / "parser"
SELFTEST_OUT = ROOT / "out" / "selftest"

# One plain run only, so expected.csv describes run 1.
SINGLE_RUN = ["--repeats", "1", "--hash-seeds", "", "--orders", "0", "--isolated", "0"]

# case -> (extra detect.sh options, expected exit code)
CASES = {
    "outcomes": ([], 0),
    "teardown_error": ([], 0),
    "param_ids": ([], 0),
    "lookalike_status": ([], 0),
    "captured_lookalike": ([], 0),
    "capture_disabled": ([], 0),
    "classes_and_dirs": ([], 0),
    "long_names": ([], 0),
    # the project's addopts asks for quiet output
    "quiet_addopts": ([], 0),
    "timeout": (["--timeout", "3"], 0),
    "crash_midrun": ([], 0),
    "exit1_midrun": ([], 0),
    # every test is not_run, so no results can be read at all
    "crash_before_tests": ([], 5),
}

# Fixtures whose tests give the same outcome every time, in any order.
DETERMINISTIC = [
    "outcomes",
    "teardown_error",
    "param_ids",
    "lookalike_status",
    "captured_lookalike",
    "capture_disabled",
    "classes_and_dirs",
    "long_names",
    "quiet_addopts",
]


def run_detect(case, *options, out_name=None):
    out_dir = SELFTEST_OUT / (out_name or case)
    result = subprocess.run(
        ["bash", str(DETECT), "--project", str(FIXTURES / case), "--no-report",
         "--out", str(out_dir), *options],
        capture_output=True,
        text=True,
        timeout=180,
    )
    return result, out_dir


def read_csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


@pytest.mark.parametrize("case", sorted(CASES))
def test_results_match_expected_csv(case):
    options, expected_exit = CASES[case]
    result, out_dir = run_detect(case, *SINGLE_RUN, *options)

    assert result.returncode == expected_exit, result.stderr
    expected = read_csv(FIXTURES / case / "expected.csv")
    actual = read_csv(out_dir / "results.csv")
    assert actual == expected


@pytest.mark.parametrize("case", DETERMINISTIC)
def test_outcomes_agree_across_all_conditions(case):
    """Order and isolated runs pass node IDs on the command line, so IDs with
    spaces, brackets, commas or quotes must survive that round trip."""
    result, out_dir = run_detect(
        case, "--repeats", "2", "--hash-seeds", "1 2", "--orders", "2", "--isolated", "1",
        out_name=case + "_matrix",
    )
    assert result.returncode == 0, result.stderr

    expected = {row["test_id"]: row["outcome"] for row in read_csv(FIXTURES / case / "expected.csv")}
    seen = defaultdict(set)
    runs_per_condition = defaultdict(set)
    for row in read_csv(out_dir / "results.csv"):
        seen[row["test_id"]].add(row["outcome"])
        runs_per_condition[row["condition"]].add(row["run_id"])

    assert {test_id: {o} for test_id, o in expected.items()} == dict(seen)
    assert {c: len(r) for c, r in runs_per_condition.items()} == {
        "repeat": 2,
        "hashseed": 2,
        "order": 3,  # reversed + 2 shuffles
        "isolated": len(expected),
    }


def test_shuffled_runs_keep_every_test_once():
    result, out_dir = run_detect(
        "param_ids", "--repeats", "1", "--hash-seeds", "", "--orders", "3", "--isolated", "0",
        out_name="param_ids_orders",
    )
    assert result.returncode == 0, result.stderr

    rows = read_csv(out_dir / "results.csv")
    collected = (out_dir / "collected.txt").read_text().splitlines()
    for run_id in {row["run_id"] for row in rows}:
        run_rows = [row for row in rows if row["run_id"] == run_id]
        assert sorted(row["test_id"] for row in run_rows) == sorted(collected)
        assert sorted(int(row["position"]) for row in run_rows) == list(range(1, len(collected) + 1))

    reverse = [row for row in rows if row["condition_value"] == "reverse"]
    assert [row["test_id"] for row in sorted(reverse, key=lambda r: int(r["position"]))] == collected[::-1]


@pytest.mark.parametrize(
    "case, status, exit_code",
    [
        ("timeout", "timeout", "124"),
        ("crash_midrun", "crashed", "9"),
        ("crash_before_tests", "crashed", "9"),
    ],
)
def test_run_meta_records_abnormal_runs(case, status, exit_code):
    options, _ = CASES[case]
    _, out_dir = run_detect(case, *SINGLE_RUN, *options, out_name=case + "_meta")
    (meta,) = read_csv(out_dir / "run_meta.csv")
    assert (meta["status"], meta["exit_code"]) == (status, exit_code)


def test_exit_code_1_crash_is_flagged():
    """os._exit(1) looks like "tests failed"; the row count gives it away."""
    result, out_dir = run_detect("exit1_midrun", *SINGLE_RUN, out_name="exit1_meta")
    (meta,) = read_csv(out_dir / "run_meta.csv")
    assert (meta["tests_expected"], meta["tests_reported"]) == ("3", "1")
    assert "reported only 1 of 3 tests" in result.stderr
