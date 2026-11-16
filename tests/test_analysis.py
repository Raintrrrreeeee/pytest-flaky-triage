"""Unit and contract tests for the statistical analysis stage."""

import csv
import json
from pathlib import Path

import pytest

from pytest_flaky_triage_analysis import InputError, analyse, load_records
from pytest_flaky_triage_analysis.__main__ import main
from pytest_flaky_triage_analysis.records import META_FIELDS, RESULT_FIELDS


def write_matrix(tmp_path, cases, test_id='test example.py::test_value[a, "b"]'):
    """Cases are (condition, value, outcome); one test per run for small fixtures."""
    results, metadata = [], []
    for run_id, (condition, value, outcome) in enumerate(cases, 1):
        timeout = outcome in ("timeout", "not_run")
        results.append([run_id, condition, value, "" if timeout else 1, test_id, outcome])
        metadata.append([run_id, condition, value, value if condition == "hashseed" else 0,
                         124 if timeout else 1 if outcome in ("failed", "error") else 0,
                         "timeout" if timeout else "ok", "" if timeout else "0.02",
                         1, 0 if timeout else 1, f"logs/run_{run_id:03}.log"])
    for name, fields, data in [("results.csv", RESULT_FIELDS, results), ("run_meta.csv", META_FIELDS, metadata)]:
        with (tmp_path / name).open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(fields)
            writer.writerows(data)
    return tmp_path / "results.csv"


def write_matrix_with_completion(tmp_path, cases, test_id="test_primary"):
    """Cases are (condition, value, primary outcome, run complete)."""
    results, metadata = [], []
    for run_id, (condition, value, outcome, complete) in enumerate(cases, 1):
        results.extend([
            [run_id, condition, value, 1, test_id, outcome],
            [run_id, condition, value, 2 if complete else "", "test_companion",
             "passed" if complete else "not_run"],
        ])
        reported = 2 if complete else 1
        exit_code = 1 if outcome in ("failed", "error") or not complete else 0
        metadata.append([
            run_id, condition, value, value if condition == "hashseed" else 0,
            exit_code, "ok", "0.02" if complete else "", 2, reported,
            f"logs/run_{run_id:03}.log",
        ])
    for name, fields, data in [("results.csv", RESULT_FIELDS, results),
                               ("run_meta.csv", META_FIELDS, metadata)]:
        with (tmp_path / name).open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(fields)
            writer.writerows(data)
    return tmp_path / "results.csv"


def read_analysis(path):
    return analyse(*load_records(path))


@pytest.mark.parametrize("outcomes,verdict,score", [
    (["passed", "passed"], "stable", 1),
    (["failed", "failed"], "consistently_failing", 0),
    (["error", "failed"], "consistently_failing", 0),
    (["passed", "failed"], "flaky", 0.5),
    (["passed", "error"], "flaky", 0.5),
    (["passed"], "inconclusive", 1),
    (["failed"], "inconclusive", 0),
    (["timeout", "not_run"], "inconclusive", None),
    (["skipped", "xfailed", "xpassed"], "inconclusive", None),
    (["passed", "passed", "timeout", "not_run", "skipped", "xfailed", "xpassed"], "stable", 1),
])
def test_analysis_verdicts_and_score(tmp_path, outcomes, verdict, score):
    path = write_matrix(tmp_path, [("repeat", str(i), outcome) for i, outcome in enumerate(outcomes, 1)])
    test = read_analysis(path)["tests"][0]
    assert test["overall"]["verdict"] == verdict
    assert test["overall"]["stability_score"] == score
    assert test["overall"]["total"] == len(outcomes)
    assert test["test_id"] == 'test example.py::test_value[a, "b"]'
    assert len(test["evidence"]) == len(outcomes)


@pytest.mark.parametrize("cases,candidates", [
    ([("repeat", "1", "passed"), ("repeat", "2", "failed")], [["unseeded_randomness", "race_conditions"]]),
    ([("repeat", "1", "passed"), ("repeat", "2", "passed"), ("hashseed", "1", "passed"), ("hashseed", "2", "failed")], [["hash_order_sensitivity"]]),
    ([("repeat", "1", "passed"), ("repeat", "2", "passed"), ("order", "reverse", "failed"), ("order", "shuffle:1", "passed")], [["shared_state_interference"]]),
    ([("repeat", "1", "passed"), ("repeat", "2", "passed"), ("order", "reverse", "failed")], [["shared_state_interference"]]),
    ([("repeat", "1", "failed"), ("repeat", "2", "failed"), ("isolated", "alone", "passed")], [["shared_state_interference"]]),
    ([("repeat", "1", "passed"), ("repeat", "2", "passed"), ("isolated", "alone", "failed")], [["shared_state_interference"]]),
    ([("repeat", "1", "passed"), ("repeat", "2", "passed"), ("tz", "UTC", "passed"), ("tz", "Pacific/Kiritimati", "failed")], [["timezone_sensitivity"]]),
    ([("repeat", "1", "passed"), ("repeat", "2", "passed"), ("repeat", "3", "passed"), ("repeat", "4", "passed"), ("repeat", "5", "passed"), ("tz", "UTC", "passed"), ("tz", "Pacific/Kiritimati", "failed"), ("tz", "Pacific/Pago_Pago", "passed")], [["timezone_sensitivity"]]),
    ([("repeat", "1", "passed"), ("repeat", "2", "passed"), ("repeat", "3", "passed"), ("repeat", "4", "passed"), ("repeat", "5", "passed"), ("hashseed", "1", "passed"), ("hashseed", "2", "failed"), ("tz", "UTC", "passed"), ("tz", "Pacific/Kiritimati", "failed")], [["unseeded_randomness", "race_conditions"]]),
    ([("repeat", "1", "passed"), ("repeat", "2", "failed"), ("hashseed", "1", "passed"), ("hashseed", "2", "failed"), ("order", "reverse", "failed"), ("isolated", "alone", "passed")], [["unseeded_randomness", "race_conditions"]]),
    ([("hashseed", "1", "passed"), ("hashseed", "2", "failed")], []),
    ([("repeat", "1", "passed"), ("repeat", "2", "passed"), ("hashseed", "1", "passed"), ("hashseed", "1", "failed")], []),
    ([("repeat", "1", "failed"), ("repeat", "2", "failed"), ("isolated", "alone", "failed")], []),
])
def test_analysis_suspicions(tmp_path, cases, candidates):
    test = read_analysis(write_matrix(tmp_path, cases))["tests"][0]
    assert [c["candidates"] for c in test["suspected_causes"]] == candidates
    ids = {r["run_id"] for r in test["evidence"]}
    for cause in test["suspected_causes"]:
        assert set(cause["run_ids"]) <= ids


def mutate(path, field, value, row=0):
    with path.open(newline="") as stream:
        reader = csv.DictReader(stream)
        fields, rows = reader.fieldnames, list(reader)
    rows[row][field] = value
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


@pytest.mark.parametrize("file,field,value", [
    ("results.csv", "outcome", "unknown"),
    ("results.csv", "position", "-1"),
    ("results.csv", "position", "2"),
    ("results.csv", "test_id", ""),
    ("results.csv", "run_id", "99"),
    ("results.csv", "condition_value", "99"),
    ("run_meta.csv", "tests_expected", "2"),
    ("run_meta.csv", "tests_reported", "0"),
    ("run_meta.csv", "hash_seed", "7"),
    ("run_meta.csv", "seconds", "nan"),
    ("run_meta.csv", "seconds", "-0.1"),
    ("run_meta.csv", "status", "crashed"),
])
def test_analysis_rejects_inconsistent_records(tmp_path, file, field, value):
    path = write_matrix(tmp_path, [("repeat", "1", "passed")])
    mutate(tmp_path / file, field, value)
    with pytest.raises(InputError):
        load_records(path)


@pytest.mark.parametrize("name", ["results.csv", "run_meta.csv"])
def test_analysis_rejects_duplicate_rows(tmp_path, name):
    path = write_matrix(tmp_path, [("repeat", "1", "passed")])
    file = tmp_path / name
    with file.open("a") as stream:
        stream.write(file.read_text().splitlines()[1] + "\n")
    with pytest.raises(InputError, match="duplicate"):
        load_records(path)


def test_analysis_preserves_completed_results_of_abnormal_run(tmp_path):
    path = write_matrix(tmp_path, [("repeat", "1", "passed"), ("repeat", "2", "failed")])
    mutate(tmp_path / "run_meta.csv", "status", "crashed", row=1)
    mutate(tmp_path / "run_meta.csv", "exit_code", "139", row=1)
    test = read_analysis(path)["tests"][0]
    assert test["overall"]["verdict"] == "flaky"
    assert not test["suspected_causes"]
    assert any("Abnormal" in w for w in test["warnings"])


def test_analysis_scores_completed_outcomes_from_incomplete_runs(tmp_path):
    path = write_matrix_with_completion(tmp_path, [
        ("repeat", "1", "passed", False),
        ("repeat", "2", "passed", False),
    ])
    test = {t["test_id"]: t for t in read_analysis(path)["tests"]}["test_primary"]
    assert test["overall"]["verdict"] == "stable"
    assert test["overall"]["stability_score"] == 1
    assert test["conditions"]["repeat"]["valid"] == 2
    assert test["conditions"]["repeat"]["stability_score"] == 1
    assert [row["run_id"] for row in test["evidence"]] == [1, 2]
    assert any("Incomplete runs excluded" in warning and "1, 2" in warning
               for warning in test["warnings"])


def test_analysis_excludes_incomplete_runs_from_repeat_instability(tmp_path):
    path = write_matrix_with_completion(tmp_path, [
        ("repeat", "1", "passed", False),
        ("repeat", "2", "failed", True),
    ])
    test = {t["test_id"]: t for t in read_analysis(path)["tests"]}["test_primary"]
    assert test["overall"]["verdict"] == "flaky"
    assert test["overall"]["stability_score"] == 0.5
    assert test["suspected_causes"] == []
    assert any("Incomplete runs excluded" in warning and "1" in warning
               for warning in test["warnings"])
    assert any("insufficient unconfounded evidence" in warning for warning in test["warnings"])


def test_analysis_incomplete_run_cannot_establish_repeat_baseline(tmp_path):
    path = write_matrix_with_completion(tmp_path, [
        ("repeat", "1", "passed", False),
        ("repeat", "2", "passed", True),
        ("hashseed", "1", "passed", True),
        ("hashseed", "2", "failed", True),
    ])
    test = {t["test_id"]: t for t in read_analysis(path)["tests"]}["test_primary"]
    assert test["overall"]["verdict"] == "flaky"
    assert test["suspected_causes"] == []
    assert any("Incomplete runs excluded" in warning for warning in test["warnings"])
    assert any("insufficient unconfounded evidence" in warning for warning in test["warnings"])


def test_analysis_completed_runs_still_establish_hash_suspicion(tmp_path):
    path = write_matrix_with_completion(tmp_path, [
        ("repeat", "1", "passed", True),
        ("repeat", "2", "passed", True),
        ("hashseed", "1", "passed", True),
        ("hashseed", "2", "failed", True),
    ])
    test = {t["test_id"]: t for t in read_analysis(path)["tests"]}["test_primary"]
    assert [cause["candidates"] for cause in test["suspected_causes"]] == [
        ["hash_order_sensitivity"]
    ]
    assert test["suspected_causes"][0]["run_ids"] == [1, 2, 3, 4]
    assert not any("Incomplete runs excluded" in warning for warning in test["warnings"])


def test_analysis_cause_run_ids_contain_only_eligible_runs(tmp_path):
    path = write_matrix_with_completion(tmp_path, [
        ("repeat", "1", "passed", True),
        ("repeat", "2", "passed", True),
        ("order", "reverse", "failed", True),
        ("order", "shuffle:1", "passed", True),
        ("order", "shuffle:2", "passed", False),
    ])
    test = {t["test_id"]: t for t in read_analysis(path)["tests"]}["test_primary"]
    assert [cause["candidates"] for cause in test["suspected_causes"]] == [
        ["shared_state_interference"]
    ]
    assert test["suspected_causes"][0]["run_ids"] == [1, 2, 3, 4]
    assert 5 not in test["suspected_causes"][0]["run_ids"]
    assert any("Incomplete runs excluded" in warning and "5" in warning
               for warning in test["warnings"])


def test_analysis_cli_output_and_failure_codes(tmp_path):
    path = write_matrix(tmp_path, [("repeat", "1", "passed"), ("repeat", "2", "failed")])
    output = tmp_path / "out"
    assert main([str(path), str(output)]) == 0
    destination = output / "diagnosis.json"
    old = destination.read_bytes()
    report = json.loads(old)
    assert report["summary"]["verdicts"]["flaky"] == 1
    assert report["schema_version"] == 1
    assert report["runs"][0]["log_path"] == "logs/run_001.log"
    assert main([str(path), str(path)]) == 5
    mutate(path, "outcome", "bogus")
    assert main([str(path), str(output)]) == 4
    assert destination.read_bytes() == old  # failed invocations do not replace old output
    assert main([str(tmp_path / "absent.csv"), str(output)]) == 3
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


@pytest.mark.parametrize("content", [
    "", "run_id,run_id\n1,1\n",
    ','.join(RESULT_FIELDS) + '\n1,repeat,"unterminated',
    ','.join(RESULT_FIELDS) + '\n1,repeat,1,1,test,passed,extra\n',
    ','.join(RESULT_FIELDS) + '\n1,repeat\n',
])
def test_analysis_cli_malformed_csv(tmp_path, content):
    path = write_matrix(tmp_path, [("repeat", "1", "passed")])
    path.write_text(content)
    assert main([str(path), str(tmp_path / "out")]) == 4


def test_analysis_failed_output_replacement_keeps_previous_result(tmp_path, monkeypatch):
    path = write_matrix(tmp_path, [("repeat", "1", "passed"), ("repeat", "2", "passed")])
    output = tmp_path / "out"
    assert main([str(path), str(output)]) == 0
    previous = (output / "diagnosis.json").read_bytes()

    def fail_replace(*args):
        raise OSError("simulated output replacement failure")

    monkeypatch.setattr("pytest_flaky_triage_analysis.__main__.os.replace", fail_replace)
    assert main([str(path), str(output)]) == 5
    assert (output / "diagnosis.json").read_bytes() == previous
    assert not list(output.glob(".diagnosis-*.tmp"))


def test_analysis_rejects_suite_identity_changes(tmp_path):
    path = write_matrix(tmp_path, [("repeat", "1", "passed"), ("repeat", "2", "passed")])
    mutate(path, "test_id", "different_test", row=1)
    with pytest.raises(InputError, match="test IDs changed"):
        load_records(path)


def test_analysis_reports_counts_and_per_condition_evidence(tmp_path):
    path = write_matrix(tmp_path, [("repeat", "1", "passed"), ("repeat", "2", "error"),
                                  ("repeat", "3", "failed"), ("isolated", "alone", "skipped")])
    test = read_analysis(path)["tests"][0]
    assert test["overall"]["valid"] == 3
    assert test["overall"]["excluded"] == 1
    assert test["overall"]["failure_frequency"] == pytest.approx(1 / 3)
    assert test["overall"]["error_frequency"] == pytest.approx(1 / 3)
    assert test["conditions"]["repeat"]["stability_score"] == pytest.approx(1 / 3)
    assert test["conditions"]["isolated"]["stability_score"] is None
    assert test["conditions"]["hashseed"]["total"] == 0
    assert test["evidence"][0]["position"] == 1


def test_analysis_supplied_sample():
    fixture = Path(__file__).parent / "fixtures" / "analysis"
    report = read_analysis(fixture / "results.csv")
    assert report["summary"] == {
        "tests": 21, "runs": 45, "records": 525,
        "verdicts": {"stable": 9, "consistently_failing": 4, "flaky": 8, "inconclusive": 0},
    }
    tests = {t["test_id"].split("::")[-1]: t for t in report["tests"]}
    for name in ("test_default_avatar_assignment", "test_select_promo_banner", "test_assign_support_agent", "test_get_daily_quote"):
        assert tests[name]["suspected_causes"][0]["candidates"] == ["unseeded_randomness", "race_conditions"]
    for name in ("test_new_cart_is_empty", "test_check_contains_item", "test_remove_item_empty", "test_check_length_is_one"):
        assert tests[name]["suspected_causes"][0]["candidates"] == ["shared_state_interference"]
    for test in tests.values():
        if test["overall"]["verdict"] != "flaky":
            assert not test["suspected_causes"]


def test_analysis_identifies_suspected_polluters(tmp_path):
    results = [
        # run 1 (repeat=1): test_victim runs at pos 1 (passes), test_polluter at pos 2
        [1, "repeat", "1", 1, "test_victim", "passed"],
        [1, "repeat", "1", 2, "test_polluter", "passed"],
        # run 2 (repeat=2): test_victim runs at pos 1 (passes), test_polluter at pos 2
        [2, "repeat", "2", 1, "test_victim", "passed"],
        [2, "repeat", "2", 2, "test_polluter", "passed"],
        # run 3 (order=reverse): test_polluter runs at pos 1, test_victim at pos 2 (fails)
        [3, "order", "reverse", 1, "test_polluter", "passed"],
        [3, "order", "reverse", 2, "test_victim", "failed"],
    ]
    metadata = [
        [1, "repeat", "1", 0, 0, "ok", "0.02", 2, 2, "logs/run_001.log"],
        [2, "repeat", "2", 0, 0, "ok", "0.02", 2, 2, "logs/run_002.log"],
        [3, "order", "reverse", 0, 1, "ok", "0.02", 2, 2, "logs/run_003.log"],
    ]
    for name, fields, data in [("results.csv", RESULT_FIELDS, results), ("run_meta.csv", META_FIELDS, metadata)]:
        with (tmp_path / name).open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(fields)
            writer.writerows(data)
    report = read_analysis(tmp_path / "results.csv")
    victim = {t["test_id"]: t for t in report["tests"]}["test_victim"]
    assert victim["suspected_causes"][0]["candidates"] == ["shared_state_interference"]
    assert victim["suspected_causes"][0]["suspected_polluters"] == ["test_polluter"]


def test_analysis_order_polluter_single_pass_with_dirty_isolated_is_shared_state(tmp_path):
    """Task 1 regression: 10 usable runs where test_polluter leaves persistent disk state
    so test_victim fails 9 times (including repeat, hashseed, and isolated) and only passes
    once in order=reverse when test_victim runs before test_polluter."""
    results = []
    metadata = []
    # Runs 1-5: repeat (test_polluter at pos 1, test_victim at pos 2 -> fails)
    # Runs 6-7: hashseed (test_polluter at pos 1, test_victim at pos 2 -> fails)
    # Run 8: order=shuffle:1 (test_polluter at pos 1, test_victim at pos 2 -> fails)
    # Run 9: order=reverse (test_victim at pos 1 -> passes, test_polluter at pos 2)
    # Run 10: isolated=alone (test_victim fails because disk file remained dirty)
    plan = [
        (1, "repeat", "1", False),
        (2, "repeat", "2", False),
        (3, "repeat", "3", False),
        (4, "repeat", "4", False),
        (5, "repeat", "5", False),
        (6, "hashseed", "1", False),
        (7, "hashseed", "2", False),
        (8, "order", "shuffle:1", False),
        (9, "order", "reverse", True),
    ]
    for run_id, cond, val, reversed_order in plan:
        seed = int(val) if cond == "hashseed" else 0
        if reversed_order:
            results.append([run_id, cond, val, 1, "test_victim", "passed"])
            results.append([run_id, cond, val, 2, "test_polluter", "passed"])
            metadata.append([run_id, cond, val, seed, 0, "ok", "0.02", 2, 2, f"logs/run_{run_id:03d}.log"])
        else:
            results.append([run_id, cond, val, 1, "test_polluter", "passed"])
            results.append([run_id, cond, val, 2, "test_victim", "failed"])
            metadata.append([run_id, cond, val, seed, 1, "ok", "0.02", 2, 2, f"logs/run_{run_id:03d}.log"])
    results.append([10, "isolated", "alone", 1, "test_victim", "failed"])
    metadata.append([10, "isolated", "alone", 0, 1, "ok", "0.02", 1, 1, "logs/run_010.log"])

    for name, fields, data in [("results.csv", RESULT_FIELDS, results), ("run_meta.csv", META_FIELDS, metadata)]:
        with (tmp_path / name).open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(fields)
            writer.writerows(data)

    report = read_analysis(tmp_path / "results.csv")
    victim = {t["test_id"]: t for t in report["tests"]}["test_victim"]
    assert victim["suspected_causes"] == [
        {
            "candidates": ["shared_state_interference"],
            "status": "suspected",
            "reason": (
                "Outcomes differ across test orders, or isolated executions pass while suite executions fail, "
                "with a consistent repeat baseline. Suspected polluting predecessor(s): test_polluter."
            ),
            "run_ids": [1, 2, 3, 4, 5, 8, 9, 10],
            "suspected_polluters": ["test_polluter"],
        }
    ]


def test_analysis_mixed_repeat_does_not_swallow_strong_isolation_or_order_polluter(tmp_path):
    """Task 2 regression: when repeat is mixed (e.g., self-pollution on repeat=2 or composite
    randomness + state pollution), strong isolation_effect or order_effect with polluters is
    preserved alongside ['unseeded_randomness', 'race_conditions']."""
    # Case A: self-pollution or suite victim that passes repeat=1, fails repeat=2, fails order, passes isolated
    path = write_matrix(
        tmp_path,
        [
            ("repeat", "1", "passed"),
            ("repeat", "2", "failed"),
            ("order", "reverse", "failed"),
            ("isolated", "alone", "passed"),
        ],
    )
    causes = read_analysis(path)["tests"][0]["suspected_causes"]
    cand_lists = [c["candidates"] for c in causes]
    assert ["unseeded_randomness", "race_conditions"] in cand_lists
    assert ["shared_state_interference"] in cand_lists

    # Case B: mixed repeat + deterministic order polluter
    sub = tmp_path / "polluter_mixed"
    sub.mkdir()
    results = [
        [1, "repeat", "1", 1, "test_victim", "passed"],
        [1, "repeat", "1", 2, "test_polluter", "passed"],
        [2, "repeat", "2", 1, "test_polluter", "passed"],
        [2, "repeat", "2", 2, "test_victim", "failed"],
        [3, "order", "shuffle:1", 1, "test_polluter", "passed"],
        [3, "order", "shuffle:1", 2, "test_victim", "failed"],
        [4, "order", "reverse", 1, "test_victim", "passed"],
        [4, "order", "reverse", 2, "test_polluter", "passed"],
    ]
    metadata = [
        [1, "repeat", "1", 0, 0, "ok", "0.02", 2, 2, "logs/run_001.log"],
        [2, "repeat", "2", 0, 1, "ok", "0.02", 2, 2, "logs/run_002.log"],
        [3, "order", "shuffle:1", 0, 1, "ok", "0.02", 2, 2, "logs/run_003.log"],
        [4, "order", "reverse", 0, 0, "ok", "0.02", 2, 2, "logs/run_004.log"],
    ]
    for name, fields, data in [("results.csv", RESULT_FIELDS, results), ("run_meta.csv", META_FIELDS, metadata)]:
        with (sub / name).open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(fields)
            writer.writerows(data)
    victim = {t["test_id"]: t for t in read_analysis(sub / "results.csv")["tests"]}["test_victim"]
    shared = [c for c in victim["suspected_causes"] if c["candidates"] == ["shared_state_interference"]]
    assert len(shared) == 1
    assert shared[0]["suspected_polluters"] == ["test_polluter"]


def test_analysis_cli_positional_and_flag_arguments(tmp_path):
    """Verify analysis.__main__.main supports both positional arguments and README flag arguments."""
    results_csv = write_matrix(tmp_path, [("repeat", "1", "passed"), ("repeat", "2", "failed")])
    run_meta_csv = tmp_path / "run_meta.csv"

    # 1. Positional invocation: python -m analysis <results_csv> <output_dir>
    pos_out_dir = tmp_path / "pos_out"
    assert main([str(results_csv), str(pos_out_dir)]) == 0
    pos_diag = json.loads((pos_out_dir / "diagnosis.json").read_text(encoding="utf-8"))
    assert pos_diag["summary"]["verdicts"]["flaky"] == 1

    # 2. README flag invocation with explicit .json output path and --run-meta:
    #    python -m analysis --results ./out/results.csv --run-meta ./out/run_meta.csv --out ./out/diagnosis.json
    flag_out_file = tmp_path / "flag_out" / "diagnosis.json"
    assert (
        main(
            [
                "--results",
                str(results_csv),
                "--run-meta",
                str(run_meta_csv),
                "--out",
                str(flag_out_file),
            ]
        )
        == 0
    )
    assert flag_out_file.is_file()
    flag_diag = json.loads(flag_out_file.read_text(encoding="utf-8"))
    assert flag_diag == pos_diag

    # 3. Flag invocation without --run-meta and with --out pointing to a directory
    dir_flag_out = tmp_path / "dir_flag_out"
    assert main(["--results", str(results_csv), "--out", str(dir_flag_out)]) == 0
    assert (dir_flag_out / "diagnosis.json").is_file()

    # 4. Missing required arguments exits with code 2
    with pytest.raises(SystemExit) as exc:
        main(["--results", str(results_csv)])
    assert exc.value.code == 2


def test_low_freq_race_with_first_repeat_pass_not_misclassified_as_shared_state(tmp_path):
    """BUG-03 regression: a low-frequency random/race test that passes repeat=1, fails
    repeat=2, and passes repeat=3..10 (plus passing hashseed, order, and isolated) must
    not be misclassified as shared_state_interference."""
    cases = [
        ("repeat", "1", "passed"),
        ("repeat", "2", "failed"),
        *[("repeat", str(i), "passed") for i in range(3, 11)],
        ("hashseed", "1", "passed"),
        ("hashseed", "2", "passed"),
        ("order", "reverse", "passed"),
        ("order", "shuffle:1", "passed"),
        ("isolated", "alone", "passed"),
    ]
    path = write_matrix(tmp_path, cases, test_id="tests/test_race.py::test_low_freq_race")
    test = read_analysis(path)["tests"][0]
    cand_lists = [c["candidates"] for c in test["suspected_causes"]]
    assert cand_lists == [["unseeded_randomness", "race_conditions"]]
    assert ["shared_state_interference"] not in cand_lists


def test_persistent_self_pollution_detected(tmp_path):
    """BUG-03 regression: when repeat=1 passes and repeat=2..10 all fail (persistent
    self-pollution across suite runs) while isolated passes, shared_state_interference
    is detected without the self-contradictory 'with a consistent repeat baseline' text."""
    cases = [
        ("repeat", "1", "passed"),
        *[("repeat", str(i), "failed") for i in range(2, 11)],
        ("isolated", "alone", "passed"),
    ]
    path = write_matrix(tmp_path, cases, test_id="tests/test_self.py::test_self_polluter")
    test = read_analysis(path)["tests"][0]
    shared = [c for c in test["suspected_causes"] if c["candidates"] == ["shared_state_interference"]]
    assert len(shared) == 1
    assert "with a consistent repeat baseline" not in shared[0]["reason"]
    assert "after initial state pollution" in shared[0]["reason"]


def test_multi_isolated_runs_mixed_detects_internal_randomness_or_race(tmp_path):
    """When suite runs all pass but multiple isolated runs of the same test vary,
    infer unseeded_randomness / race_conditions rather than consumer_isolation."""
    cases = [
        ("repeat", "1", "passed"),
        ("repeat", "2", "passed"),
        ("repeat", "3", "passed"),
        ("hashseed", "1", "passed"),
        ("order", "reverse", "passed"),
        ("tz", "UTC", "passed"),
        ("isolated", "alone", "passed"),
        ("isolated", "alone", "passed"),
        ("isolated", "alone", "failed"),
        ("isolated", "alone", "passed"),
        ("isolated", "alone", "passed"),
    ]
    path = write_matrix(tmp_path, cases, test_id="tests/test_iso.py::test_iso_mixed")
    test = read_analysis(path)["tests"][0]
    assert test["overall"]["verdict"] == "flaky"
    assert test["suspected_causes"] == [
        {
            "candidates": ["unseeded_randomness", "race_conditions"],
            "status": "ambiguous",
            "reason": "Outcomes vary across repeated isolated executions of this single test under the same seed and timezone.",
            "run_ids": [7, 8, 9, 10, 11],
        }
    ]


def test_multi_isolated_runs_failure_prevents_false_victim_isolation(tmp_path):
    """When a test fails in suite runs and has at least one failure across multiple
    isolated runs (mixed isolated outcomes), it must not be misclassified as victim_isolation."""
    cases = [
        ("repeat", "1", "failed"),
        ("repeat", "2", "failed"),
        ("order", "reverse", "failed"),
        ("isolated", "alone", "passed"),
        ("isolated", "alone", "passed"),
        ("isolated", "alone", "failed"),
        ("isolated", "alone", "passed"),
        ("isolated", "alone", "passed"),
    ]
    path = write_matrix(tmp_path, cases, test_id="tests/test_iso.py::test_false_victim")
    test = read_analysis(path)["tests"][0]
    assert test["overall"]["verdict"] == "flaky"
    cand_lists = [c["candidates"] for c in test["suspected_causes"]]
    assert cand_lists == [["unseeded_randomness", "race_conditions"]]
    assert ["shared_state_interference"] not in cand_lists


def test_multi_isolated_runs_consistent_failure_confirms_consumer_isolation(tmp_path):
    """When a test passes in suite runs and fails consistently across all N isolated runs,
    confirm consumer_isolation (shared_state_interference)."""
    cases = [
        ("repeat", "1", "passed"),
        ("repeat", "2", "passed"),
        ("order", "reverse", "passed"),
        ("isolated", "alone", "failed"),
        ("isolated", "alone", "failed"),
        ("isolated", "alone", "failed"),
        ("isolated", "alone", "failed"),
        ("isolated", "alone", "failed"),
    ]
    path = write_matrix(tmp_path, cases, test_id="tests/test_iso.py::test_state_consumer")
    test = read_analysis(path)["tests"][0]
    assert test["overall"]["verdict"] == "flaky"
    shared = [c for c in test["suspected_causes"] if c["candidates"] == ["shared_state_interference"]]
    assert len(shared) == 1
    assert "isolated executions differ from suite executions" in shared[0]["reason"]


def test_disjunctive_multi_polluters_and_one_way_polluter_preserved(tmp_path):
    """BUG-07 regression:
    Scenario A: two independent one-way polluters (test_polluter_a, test_polluter_b) alternate
    preceding test_victim across failed runs; both must be identified via the disjunctive net.
    Scenario B: when test_polluter_b is a mutual polluter with test_victim while test_polluter_a
    is a one-way polluter that alone explains another failed run, test_polluter_a must not be
    erased by mutual polluter refinement."""
    # Scenario A: two one-way polluters taking turns
    dir_a = tmp_path / "scenario_a"
    dir_a.mkdir()
    orders_a = [
        (1, "repeat", "1", [("test_victim", "passed"), ("test_polluter_a", "passed"), ("test_polluter_b", "passed")]),
        (2, "repeat", "2", [("test_victim", "passed"), ("test_polluter_a", "passed"), ("test_polluter_b", "passed")]),
        (3, "order", "shuffle:1", [("test_polluter_a", "passed"), ("test_victim", "failed"), ("test_polluter_b", "passed")]),
        (4, "order", "shuffle:2", [("test_polluter_b", "passed"), ("test_victim", "failed"), ("test_polluter_a", "passed")]),
        (5, "order", "shuffle:3", [("test_victim", "passed"), ("test_polluter_a", "passed"), ("test_polluter_b", "passed")]),
    ]
    results_a, meta_a = [], []
    for run_id, cond, val, seq in orders_a:
        any_fail = any(out != "passed" for _, out in seq)
        for pos, (tid, out) in enumerate(seq, 1):
            results_a.append([run_id, cond, val, pos, tid, out])
        meta_a.append([run_id, cond, val, 0, 1 if any_fail else 0, "ok", "0.02", 3, 3, f"logs/run_{run_id:03d}.log"])
    for name, fields, data in [("results.csv", RESULT_FIELDS, results_a), ("run_meta.csv", META_FIELDS, meta_a)]:
        with (dir_a / name).open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(fields)
            writer.writerows(data)
    report_a = read_analysis(dir_a / "results.csv")
    victim_a = {t["test_id"]: t for t in report_a["tests"]}["test_victim"]
    polluters_a = victim_a["suspected_causes"][0]["suspected_polluters"]
    assert "test_polluter_a" in polluters_a
    assert "test_polluter_b" in polluters_a

    # Scenario B: mutual polluter (test_polluter_b <-> test_victim) + one-way polluter (test_polluter_a -> test_victim)
    dir_b = tmp_path / "scenario_b"
    dir_b.mkdir()
    orders_b = [
        (1, "repeat", "1", [("test_victim", "passed"), ("test_polluter_b", "failed"), ("test_polluter_a", "passed")]),
        (2, "repeat", "2", [("test_victim", "passed"), ("test_polluter_b", "failed"), ("test_polluter_a", "passed")]),
        (3, "order", "shuffle:1", [("test_polluter_a", "passed"), ("test_victim", "failed"), ("test_polluter_b", "failed")]),
        (4, "order", "shuffle:2", [("test_polluter_b", "passed"), ("test_victim", "failed"), ("test_polluter_a", "passed")]),
        (5, "order", "shuffle:3", [("test_victim", "passed"), ("test_polluter_a", "passed"), ("test_polluter_b", "failed")]),
    ]
    results_b, meta_b = [], []
    for run_id, cond, val, seq in orders_b:
        any_fail = any(out != "passed" for _, out in seq)
        for pos, (tid, out) in enumerate(seq, 1):
            results_b.append([run_id, cond, val, pos, tid, out])
        meta_b.append([run_id, cond, val, 0, 1 if any_fail else 0, "ok", "0.02", 3, 3, f"logs/run_{run_id:03d}.log"])
    for name, fields, data in [("results.csv", RESULT_FIELDS, results_b), ("run_meta.csv", META_FIELDS, meta_b)]:
        with (dir_b / name).open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(fields)
            writer.writerows(data)
    report_b = read_analysis(dir_b / "results.csv")
    tests_b = {t["test_id"]: t for t in report_b["tests"]}
    victim_b = tests_b["test_victim"]
    polluters_b = victim_b["suspected_causes"][0]["suspected_polluters"]
    assert "test_polluter_a" in polluters_b
    assert "test_polluter_b" in polluters_b


def test_parameterized_polluter_name_with_double_colon_not_truncated(tmp_path):
    """BUG-09 regression: parameterized polluter test IDs containing '::' inside brackets
    must preserve the full function and parameter suffix in reason strings."""
    polluter_id = "tests/test_mod.py::test_polluter[ns::key]"
    victim_id = "tests/test_mod.py::test_victim"
    results = [
        [1, "repeat", "1", 1, victim_id, "passed"],
        [1, "repeat", "1", 2, polluter_id, "passed"],
        [2, "repeat", "2", 1, victim_id, "passed"],
        [2, "repeat", "2", 2, polluter_id, "passed"],
        [3, "order", "reverse", 1, polluter_id, "passed"],
        [3, "order", "reverse", 2, victim_id, "failed"],
    ]
    metadata = [
        [1, "repeat", "1", 0, 0, "ok", "0.02", 2, 2, "logs/run_001.log"],
        [2, "repeat", "2", 0, 0, "ok", "0.02", 2, 2, "logs/run_002.log"],
        [3, "order", "reverse", 0, 1, "ok", "0.02", 2, 2, "logs/run_003.log"],
    ]
    for name, fields, data in [("results.csv", RESULT_FIELDS, results), ("run_meta.csv", META_FIELDS, metadata)]:
        with (tmp_path / name).open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(fields)
            writer.writerows(data)
    report = read_analysis(tmp_path / "results.csv")
    victim = {t["test_id"]: t for t in report["tests"]}[victim_id]
    reason = victim["suspected_causes"][0]["reason"]
    assert "Suspected polluting predecessor(s): test_polluter[ns::key]." in reason
    assert "Suspected polluting predecessor(s): key]." not in reason


def test_random_test_flipping_in_repeat_and_passing_isolated_not_misclassified_as_shared_state(tmp_path):
    """Task 8.1 regression: a random/race test that flips during repeat (under the exact same
    predecessor set) and happens to pass in isolated runs (or fail in some shuffled orders
    after another test) must NOT be misclassified as shared_state_interference."""
    results = [
        # repeat 1: passes at pos 1
        [1, "repeat", "1", 1, "test_random_flaky", "passed"],
        [1, "repeat", "1", 2, "test_bystander", "passed"],
        # repeat 2: fails at pos 1 (same empty predecessor set as repeat 1!)
        [2, "repeat", "2", 1, "test_random_flaky", "failed"],
        [2, "repeat", "2", 2, "test_bystander", "passed"],
        # hashseed runs: mixed
        [3, "hashseed", "1", 1, "test_random_flaky", "passed"],
        [3, "hashseed", "1", 2, "test_bystander", "passed"],
        [4, "hashseed", "2", 1, "test_random_flaky", "failed"],
        [4, "hashseed", "2", 2, "test_bystander", "passed"],
        # order reverse: test_bystander precedes test_random_flaky, which happens to fail
        [5, "order", "reverse", 1, "test_bystander", "passed"],
        [5, "order", "reverse", 2, "test_random_flaky", "failed"],
        # isolated runs: happens to pass
        [6, "isolated", "alone", 1, "test_random_flaky", "passed"],
        [7, "isolated", "alone", 1, "test_random_flaky", "passed"],
    ]
    metadata = [
        [1, "repeat", "1", 0, 0, "ok", "0.02", 2, 2, "logs/run_001.log"],
        [2, "repeat", "2", 0, 1, "ok", "0.02", 2, 2, "logs/run_002.log"],
        [3, "hashseed", "1", 1, 0, "ok", "0.02", 2, 2, "logs/run_003.log"],
        [4, "hashseed", "2", 2, 1, "ok", "0.02", 2, 2, "logs/run_004.log"],
        [5, "order", "reverse", 0, 1, "ok", "0.02", 2, 2, "logs/run_005.log"],
        [6, "isolated", "alone", 0, 0, "ok", "0.01", 1, 1, "logs/run_006.log"],
        [7, "isolated", "alone", 0, 0, "ok", "0.01", 1, 1, "logs/run_007.log"],
    ]
    for name, fields, data in [("results.csv", RESULT_FIELDS, results), ("run_meta.csv", META_FIELDS, metadata)]:
        with (tmp_path / name).open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(fields)
            writer.writerows(data)
    report = read_analysis(tmp_path / "results.csv")
    flaky = {t["test_id"]: t for t in report["tests"]}["test_random_flaky"]
    causes = [c["candidates"] for c in flaky["suspected_causes"]]
    assert causes == [["unseeded_randomness", "race_conditions"]]





