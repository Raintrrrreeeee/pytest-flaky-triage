"""Unit and integration tests for the report generator and AST heuristics."""

import copy
import json
import subprocess
import sys
from pathlib import Path
import pytest

from report import (
    ReportContractError,
    disambiguate_causes,
    generate_report,
    inspect_test_source,
    load_diagnosis,
    render_markdown,
)
from report.__main__ import main


def _counts(passed=0, failed=0, error=0, skipped=0, xfailed=0, xpassed=0, timeout=0, not_run=0):
    return {
        "passed": passed,
        "failed": failed,
        "error": error,
        "skipped": skipped,
        "xfailed": xfailed,
        "xpassed": xpassed,
        "timeout": timeout,
        "not_run": not_run,
    }


def _summary_block(
    passed=5,
    failed=0,
    error=0,
    skipped=0,
    xfailed=0,
    xpassed=0,
    timeout=0,
    not_run=0,
    verdict="stable",
):
    valid = passed + failed + error
    excluded = skipped + xfailed + xpassed
    invalid = timeout + not_run
    total = valid + excluded + invalid
    score = (passed / valid) if valid > 0 else None
    fail_freq = (failed / valid) if valid > 0 else None
    err_freq = (error / valid) if valid > 0 else None
    return {
        "total": total,
        "valid": valid,
        "counts": _counts(
            passed=passed,
            failed=failed,
            error=error,
            skipped=skipped,
            xfailed=xfailed,
            xpassed=xpassed,
            timeout=timeout,
            not_run=not_run,
        ),
        "excluded": excluded,
        "invalid": invalid,
        "stability_score": score,
        "failure_frequency": fail_freq,
        "error_frequency": err_freq,
        "verdict": verdict,
    }


def _sample_diagnosis():
    return {
        "schema_version": 1,
        "policy": {
            "stability_score": "passed / (passed + failed + error)",
            "failure_frequency": "failed / (passed + failed + error)",
            "error_frequency": "error / (passed + failed + error)",
            "excluded_from_denominator": ["skipped", "xfailed", "xpassed"],
            "invalid_outcomes": ["timeout", "not_run"],
            "minimum_valid_runs": 2,
            "scope": (
                "Scores and verdicts describe observed outcomes across the "
                "configured runs. Cause categories are evidence-backed "
                "suspicions, not proven root causes."
            ),
        },
        "summary": {
            "tests": 2,
            "runs": 2,
            "records": 4,
            "verdicts": {
                "stable": 1,
                "consistently_failing": 0,
                "flaky": 1,
                "inconclusive": 0,
            },
        },
        "runs": [
            {
                "run_id": 1,
                "condition": "repeat",
                "condition_value": "1",
                "hash_seed": 0,
                "exit_code": 1,
                "status": "ok",
                "seconds": 0.12,
                "tests_expected": 2,
                "tests_reported": 2,
                "log_path": "logs/run_001.log",
            },
            {
                "run_id": 2,
                "condition": "repeat",
                "condition_value": "2",
                "hash_seed": 0,
                "exit_code": 0,
                "status": "ok",
                "seconds": 0.11,
                "tests_expected": 2,
                "tests_reported": 2,
                "log_path": "logs/run_002.log",
            },
        ],
        "tests": [
            {
                "test_id": "tests/test_sample.py::test_stable",
                "overall": _summary_block(passed=2, failed=0, verdict="stable"),
                "conditions": {
                    "repeat": _summary_block(passed=2, failed=0, verdict="stable"),
                    "hashseed": _summary_block(passed=0, failed=0, verdict="inconclusive"),
                    "order": _summary_block(passed=0, failed=0, verdict="inconclusive"),
                    "isolated": _summary_block(passed=0, failed=0, verdict="inconclusive"),
                },
                "suspected_causes": [],
                "warnings": [],
                "evidence": [
                    {"run_id": 1, "position": 1, "outcome": "passed"},
                    {"run_id": 2, "position": 1, "outcome": "passed"},
                ],
            },
            {
                "test_id": "tests/test_sample.py::test_random_flaky",
                "overall": _summary_block(passed=1, failed=1, verdict="flaky"),
                "conditions": {
                    "repeat": _summary_block(passed=1, failed=1, verdict="flaky"),
                    "hashseed": _summary_block(passed=0, failed=0, verdict="inconclusive"),
                    "order": _summary_block(passed=0, failed=0, verdict="inconclusive"),
                    "isolated": _summary_block(passed=0, failed=0, verdict="inconclusive"),
                },
                "suspected_causes": [
                    {
                        "candidates": ["unseeded_randomness", "race_conditions"],
                        "status": "ambiguous",
                        "reason": "Passed and failed across repeated runs with identical order and PYTHONHASHSEED.",
                        "run_ids": [1, 2],
                    }
                ],
                "warnings": [],
                "evidence": [
                    {"run_id": 1, "position": 2, "outcome": "failed"},
                    {"run_id": 2, "position": 2, "outcome": "passed"},
                ],
            },
        ],
    }


def test_schema_validation_accepts_valid_and_rejects_malformed(tmp_path):
    diag = _sample_diagnosis()
    diag_path = tmp_path / "diagnosis.json"
    diag_path.write_text(json.dumps(diag), encoding="utf-8")
    loaded = load_diagnosis(diag_path)
    assert loaded["schema_version"] == 1

    bad_version = copy.deepcopy(diag)
    bad_version["schema_version"] = 2
    with pytest.raises(ReportContractError, match="schema_version"):
        generate_report(bad_version)

    bool_version = copy.deepcopy(diag)
    bool_version["schema_version"] = True
    with pytest.raises(ReportContractError, match="schema_version"):
        generate_report(bool_version)

    bad_verdict = copy.deepcopy(diag)
    bad_verdict["tests"][0]["overall"]["verdict"] = "unknown_verdict"
    with pytest.raises(ReportContractError, match="verdict"):
        generate_report(bad_verdict)


def test_schema_validation_rejects_summary_block_inconsistencies():
    """Required fix 1: reject contradictory counts, scores, frequencies, and verdicts."""
    base = _sample_diagnosis()

    # 1. Incorrect total
    bad_total = copy.deepcopy(base)
    bad_total["tests"][0]["overall"]["total"] = 99
    with pytest.raises(ReportContractError, match="total"):
        render_markdown(bad_total)

    # 2. Incorrect valid / excluded / invalid counts
    bad_valid = copy.deepcopy(base)
    bad_valid["tests"][0]["overall"]["valid"] = 1
    with pytest.raises(ReportContractError, match="valid"):
        render_markdown(bad_valid)

    bad_excluded = copy.deepcopy(base)
    bad_excluded["tests"][0]["overall"]["excluded"] = 3
    with pytest.raises(ReportContractError, match="excluded"):
        render_markdown(bad_excluded)

    bad_invalid = copy.deepcopy(base)
    bad_invalid["tests"][0]["overall"]["invalid"] = 2
    with pytest.raises(ReportContractError, match="invalid"):
        render_markdown(bad_invalid)

    # 3. Reviewer's exact reproduction: passed=1, valid=2, stability_score=1.0, verdict=stable
    contradictory = copy.deepcopy(base)
    contradictory["tests"][1]["overall"]["stability_score"] = 1.0
    contradictory["tests"][1]["overall"]["verdict"] = "stable"
    with pytest.raises(ReportContractError, match="stability_score"):
        render_markdown(contradictory)

    # Incorrect failure_frequency and error_frequency
    bad_fail_freq = copy.deepcopy(base)
    bad_fail_freq["tests"][1]["overall"]["failure_frequency"] = 0.99
    with pytest.raises(ReportContractError, match="failure_frequency"):
        render_markdown(bad_fail_freq)

    bad_err_freq = copy.deepcopy(base)
    bad_err_freq["tests"][1]["overall"]["error_frequency"] = 0.50
    with pytest.raises(ReportContractError, match="error_frequency"):
        render_markdown(bad_err_freq)

    # 4. Incorrect verdict relative to counts (e.g., mixed outcomes marked stable)
    bad_verdict_policy = copy.deepcopy(base)
    bad_verdict_policy["tests"][1]["overall"]["verdict"] = "stable"
    with pytest.raises(ReportContractError, match="disagrees with counts-derived verdict"):
        render_markdown(bad_verdict_policy)

    # Fewer than 2 valid runs marked stable instead of inconclusive
    bad_inconc = copy.deepcopy(base)
    bad_inconc["tests"][0]["overall"] = _summary_block(passed=1, failed=0, verdict="stable")
    with pytest.raises(ReportContractError, match="inconclusive"):
        render_markdown(bad_inconc)

    # 5. Non-null score when valid == 0
    non_null_zero = copy.deepcopy(base)
    non_null_zero["tests"][0]["conditions"]["hashseed"]["stability_score"] = 0.0
    with pytest.raises(ReportContractError, match="must be null when valid == 0"):
        render_markdown(non_null_zero)


def test_schema_validation_rejects_cross_field_and_reference_inconsistencies():
    """Required fix 1: reject top-level summary mismatches, duplicate IDs, and broken references."""
    base = _sample_diagnosis()

    # 1. Summary test / run / record count mismatches
    bad_sum_tests = copy.deepcopy(base)
    bad_sum_tests["summary"]["tests"] = 5
    with pytest.raises(ReportContractError, match="summary.tests"):
        generate_report(bad_sum_tests)

    bad_sum_runs = copy.deepcopy(base)
    bad_sum_runs["summary"]["runs"] = 9
    with pytest.raises(ReportContractError, match="summary.runs"):
        generate_report(bad_sum_runs)

    bad_sum_records = copy.deepcopy(base)
    bad_sum_records["summary"]["records"] = 99
    with pytest.raises(ReportContractError, match="summary.records"):
        generate_report(bad_sum_records)

    # 2. Summary verdict-count mismatch
    bad_sum_verdicts = copy.deepcopy(base)
    bad_sum_verdicts["summary"]["verdicts"]["stable"] = 2
    with pytest.raises(ReportContractError, match="summary.verdicts.stable"):
        generate_report(bad_sum_verdicts)

    # 3. Duplicate run IDs
    dup_run = copy.deepcopy(base)
    dup_run["runs"][1]["run_id"] = 1
    with pytest.raises(ReportContractError, match="duplicate run_id"):
        generate_report(dup_run)

    # 4. Duplicate test IDs
    dup_test = copy.deepcopy(base)
    dup_test["tests"][1]["test_id"] = dup_test["tests"][0]["test_id"]
    with pytest.raises(ReportContractError, match="duplicate test_id"):
        generate_report(dup_test)

    # 5. Evidence referencing nonexistent run or duplicate run in same test
    bad_ev_run = copy.deepcopy(base)
    bad_ev_run["tests"][0]["evidence"][0]["run_id"] = 999
    with pytest.raises(ReportContractError, match="does not reference a valid run"):
        generate_report(bad_ev_run)

    dup_ev_run = copy.deepcopy(base)
    dup_ev_run["tests"][0]["evidence"][1]["run_id"] = 1
    with pytest.raises(ReportContractError, match="duplicate run_id"):
        generate_report(dup_ev_run)

    # 6. Suspected cause referencing nonexistent run, duplicate candidates, or duplicate run_ids
    bad_cause_run = copy.deepcopy(base)
    bad_cause_run["tests"][1]["suspected_causes"][0]["run_ids"] = [1, 999]
    with pytest.raises(ReportContractError, match="run_ids invalid"):
        generate_report(bad_cause_run)

    dup_cause_run = copy.deepcopy(base)
    dup_cause_run["tests"][1]["suspected_causes"][0]["run_ids"] = [1, 1]
    with pytest.raises(ReportContractError, match="run_ids invalid"):
        generate_report(dup_cause_run)

    dup_cause_cand = copy.deepcopy(base)
    dup_cause_cand["tests"][1]["suspected_causes"][0]["candidates"] = [
        "unseeded_randomness",
        "unseeded_randomness",
    ]
    with pytest.raises(ReportContractError, match="candidates invalid"):
        generate_report(dup_cause_cand)

    bad_cause_status = copy.deepcopy(base)
    bad_cause_status["tests"][1]["suspected_causes"][0]["status"] = "suspected"
    with pytest.raises(ReportContractError, match="disagrees with candidates count"):
        generate_report(bad_cause_status)

    # 7. Stable test with unexpected suspected_causes
    stable_with_cause = copy.deepcopy(base)
    stable_with_cause["tests"][0]["suspected_causes"] = [
        {
            "candidates": ["hash_order_sensitivity"],
            "status": "suspected",
            "reason": "Should not exist on stable test.",
            "run_ids": [1, 2],
        }
    ]
    with pytest.raises(ReportContractError, match="suspected_causes must be empty"):
        generate_report(stable_with_cause)

    # 8. Invalid run metadata status or reported > expected
    bad_run_status = copy.deepcopy(base)
    bad_run_status["runs"][0]["status"] = "failed"
    with pytest.raises(ReportContractError, match="status invalid"):
        generate_report(bad_run_status)

    bad_reported = copy.deepcopy(base)
    bad_reported["runs"][0]["tests_reported"] = 5
    with pytest.raises(ReportContractError, match="tests_reported"):
        generate_report(bad_reported)


def test_missing_or_invalid_source_never_infers_fault_side(tmp_path):
    """Required fix 2: missing, unreadable, or unparseable source yields Undetermined fault_side."""
    hash_cause = [
        {
            "candidates": ["hash_order_sensitivity"],
            "status": "suspected",
            "reason": "Passed in repeat, failed in hashseed.",
            "run_ids": [1, 2],
        }
    ]
    shared_cause = [
        {
            "candidates": ["shared_state_interference"],
            "status": "suspected",
            "reason": "Flipped across orders.",
            "run_ids": [1, 2],
        }
    ]

    # 1 & 2: Missing source with hash_order_sensitivity and shared_state_interference
    missing_info = inspect_test_source("tests/nonexistent.py::test_missing", project_dir=tmp_path)
    assert missing_info["available"] is False

    for cause in (hash_cause, shared_cause):
        res = disambiguate_causes(cause, missing_info)
        assert res["resolved_candidates"] == cause[0]["candidates"]
        assert res["disambiguated"] is False
        assert res["fault_side"] == "Undetermined"
        assert res["heuristic_notes"] == []
        assert res["compact_hints"] == []
        assert res["basis"] == "Runtime"

    # 3: Syntactically invalid source file and invalid UTF-8 encoded source file
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_syntax_err.py").write_text("def test_broken(:\n    pass\n", encoding="utf-8")
    (tests_dir / "test_encoding_err.py").write_bytes(b"\xff\xfe\x00\x00invalid utf-8")

    for bad_test_id in (
        "tests/test_syntax_err.py::test_broken",
        "tests/test_encoding_err.py::test_broken",
    ):
        bad_info = inspect_test_source(bad_test_id, project_dir=tmp_path)
        assert bad_info["available"] is False
        res = disambiguate_causes(hash_cause, bad_info)
        assert res["fault_side"] == "Undetermined"
        assert res["compact_hints"] == []

    # 4: Render full Markdown report without AST indicators and assert no 'Test Side (typical)'
    diag = _sample_diagnosis()
    diag["tests"][1]["suspected_causes"] = hash_cause
    md_no_proj = render_markdown(diag, project_dir=None)
    assert "Test Side (typical)" not in md_no_proj
    assert "Source not available for AST check" in md_no_proj


def test_conflicting_ast_hints_and_three_no_hint_states(tmp_path):
    """Verify conflicting AST hints are preserved without overriding runtime causes, and 3 no-hint states."""
    scenario_dir = Path(__file__).resolve().parents[1] / "scenario"
    info_mouse = inspect_test_source(
        "tests/test_cart.py::test_checkout_deducts_mouse",
        project_dir=scenario_dir,
    )
    shared_cause = [
        {
            "status": "suspected",
            "candidates": ["shared_state_interference"],
            "reason": "Outcomes differ across test orders.",
            "run_ids": [1, 2],
        }
    ]

    # 1. disambiguate_causes with shared_state_interference + race_conditions AST indicator
    res = disambiguate_causes(shared_cause, info_mouse)
    assert res["resolved_candidates"] == ["shared_state_interference"]
    assert res["disambiguated"] is False
    assert res["fault_side"] == "Undetermined"
    assert res["compact_hints"] == []
    assert len(res["conflicting_hints"]) == 1
    assert "Conflicting hint:" in res["conflicting_hints"][0]
    assert "race_conditions" in res["conflicting_hints"][0]
    assert "runtime suggests `shared_state_interference`" in res["conflicting_hints"][0]
    assert res["basis"] == "Runtime (AST conflict)"

    # 2. render_markdown where runtime cause disagrees with AST indicator
    diag = _sample_diagnosis()
    diag["tests"][1]["test_id"] = "tests/test_cart.py::test_checkout_deducts_mouse"
    diag["tests"][1]["suspected_causes"] = shared_cause
    md_conflict = render_markdown(diag, project_dir=scenario_dir)
    assert "Conflicting hint:" in md_conflict
    assert "Runtime (AST conflict)" in md_conflict
    assert "No static AST indicator" not in md_conflict
    assert "Source not available for AST check" not in md_conflict
    assert "No matching AST pattern" not in md_conflict

    # 3. render_markdown with project_dir=None -> "Source not available for AST check"
    md_unavail = render_markdown(diag, project_dir=None)
    assert "Source not available for AST check" in md_unavail
    assert "No matching AST pattern" not in md_unavail

    # 4. render_markdown with a project where the test has no AST pattern -> "No matching AST pattern"
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_plain.py").write_text(
        "def test_plain_arithmetic():\n"
        "    x = 1 + 1\n"
        "    assert x == 2\n",
        encoding="utf-8",
    )
    diag_plain = _sample_diagnosis()
    diag_plain["tests"][1]["test_id"] = "tests/test_plain.py::test_plain_arithmetic"
    diag_plain["tests"][1]["suspected_causes"] = shared_cause
    md_no_pattern = render_markdown(diag_plain, project_dir=tmp_path)
    assert "No matching AST pattern" in md_no_pattern
    assert "Source not available for AST check" not in md_no_pattern
    assert "No static AST indicator" not in md_no_pattern


def test_grouped_sections_preserve_per_test_evidence_and_reasons():
    """Required fix 3: preserve each test's distinct cause run IDs and reasons in grouped sections."""
    diag = _sample_diagnosis()
    diag["runs"].extend([
        {
            "run_id": 3,
            "condition": "isolated",
            "condition_value": "tests/test_sample.py::test_shared_a",
            "hash_seed": 0,
            "exit_code": 0,
            "status": "ok",
            "seconds": 0.05,
            "tests_expected": 1,
            "tests_reported": 1,
            "log_path": "logs/run_003_iso_a.log",
        },
        {
            "run_id": 4,
            "condition": "isolated",
            "condition_value": "tests/test_sample.py::test_shared_b",
            "hash_seed": 0,
            "exit_code": 0,
            "status": "ok",
            "seconds": 0.05,
            "tests_expected": 1,
            "tests_reported": 1,
            "log_path": "logs/run_004_iso_b.log",
        },
    ])
    diag["summary"] = {
        "tests": 2,
        "runs": 4,
        "records": 6,
        "verdicts": {
            "stable": 0,
            "consistently_failing": 0,
            "flaky": 2,
            "inconclusive": 0,
        },
    }
    diag["tests"] = [
        {
            "test_id": "tests/test_sample.py::test_shared_a",
            "overall": _summary_block(passed=2, failed=1, verdict="flaky"),
            "conditions": {
                "repeat": _summary_block(passed=1, failed=1, verdict="flaky"),
                "hashseed": _summary_block(passed=0, failed=0, verdict="inconclusive"),
                "order": _summary_block(passed=0, failed=0, verdict="inconclusive"),
                "isolated": _summary_block(passed=1, failed=0, verdict="inconclusive"),
            },
            "suspected_causes": [
                {
                    "candidates": ["shared_state_interference"],
                    "status": "suspected",
                    "reason": "Reason A: failed in suite order but passed in isolated run 3.",
                    "run_ids": [1, 3],
                }
            ],
            "warnings": [],
            "evidence": [
                {"run_id": 1, "position": 1, "outcome": "failed"},
                {"run_id": 2, "position": 1, "outcome": "passed"},
                {"run_id": 3, "position": 1, "outcome": "passed"},
            ],
        },
        {
            "test_id": "tests/test_sample.py::test_shared_b",
            "overall": _summary_block(passed=2, failed=1, verdict="flaky"),
            "conditions": {
                "repeat": _summary_block(passed=1, failed=1, verdict="flaky"),
                "hashseed": _summary_block(passed=0, failed=0, verdict="inconclusive"),
                "order": _summary_block(passed=0, failed=0, verdict="inconclusive"),
                "isolated": _summary_block(passed=1, failed=0, verdict="inconclusive"),
            },
            "suspected_causes": [
                {
                    "candidates": ["shared_state_interference"],
                    "status": "suspected",
                    "reason": "Reason B: order flip observed and verified in isolated run 4.",
                    "run_ids": [2, 4],
                }
            ],
            "warnings": [],
            "evidence": [
                {"run_id": 1, "position": 2, "outcome": "passed"},
                {"run_id": 2, "position": 2, "outcome": "failed"},
                {"run_id": 4, "position": 1, "outcome": "passed"},
            ],
        },
    ]

    md = render_markdown(diag, project_dir=None)
    assert "Reason A: failed in suite order but passed in isolated run 3." in md
    assert "Reason B: order flip observed and verified in isolated run 4." in md
    assert "run 3 (`logs/run_003_iso_a.log`)" in md
    assert "run 4 (`logs/run_004_iso_b.log`)" in md

    # Verify each test row in Section 3 carries its own isolated run log path
    row_a = [line for line in md.splitlines() if "`test_shared_a`" in line and "logs/" in line]
    row_b = [line for line in md.splitlines() if "`test_shared_b`" in line and "logs/" in line]
    assert len(row_a) == 1 and "logs/run_003_iso_a.log" in row_a[0]
    assert "logs/run_004_iso_b.log" not in row_a[0]
    assert len(row_b) == 1 and "logs/run_004_iso_b.log" in row_b[0]
    assert "logs/run_003_iso_a.log" not in row_b[0]


def test_ast_heuristics_disambiguate_randomness_and_race_and_ignore_joined_threads(tmp_path):
    """Verify AST heuristics distinguish unjoined threads from synchronized joined threads/locks."""
    src_dir = tmp_path / "src" / "pkg"
    src_dir.mkdir(parents=True)
    (src_dir / "__init__.py").write_text("", encoding="utf-8")
    (src_dir / "service.py").write_text(
        "import random\n"
        "import threading\n"
        "import time\n\n"
        "class Worker:\n"
        "    def pick_token(self):\n"
        "        return random.choice(['a', 'b'])\n\n"
        "    def run_unjoined_race(self):\n"
        "        def _step():\n"
        "            time.sleep(0.001)\n"
        "        t = threading.Thread(target=_step)\n"
        "        t.start()\n\n"
        "    def run_synchronized(self):\n"
        "        lock = threading.Lock()\n"
        "        evt = threading.Event()\n"
        "        def _step():\n"
        "            with lock:\n"
        "                evt.set()\n"
        "        t = threading.Thread(target=_step)\n"
        "        t.start()\n"
        "        t.join()\n\n"
        "    def tags(self):\n"
        "        return set(['x', 'y'])\n",
        encoding="utf-8",
    )

    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_worker.py").write_text(
        "from pkg.service import Worker\n\n"
        "shared_worker = Worker()\n\n"
        "def test_rand():\n"
        "    w = Worker()\n"
        "    assert w.pick_token() == 'a'\n\n"
        "def test_race_unjoined():\n"
        "    w = Worker()\n"
        "    w.run_unjoined_race()\n\n"
        "def test_synchronized_joined():\n"
        "    w = Worker()\n"
        "    w.run_synchronized()\n\n"
        "def test_hash():\n"
        "    w = Worker()\n"
        "    assert ','.join(w.tags()) == 'x,y'\n\n"
        "def test_shared():\n"
        "    shared_worker.pick_token()\n",
        encoding="utf-8",
    )

    ambiguous_cause = [
        {
            "candidates": ["unseeded_randomness", "race_conditions"],
            "status": "ambiguous",
            "reason": "Failed in repeat.",
            "run_ids": [1, 2],
        }
    ]

    info_rand = inspect_test_source("tests/test_worker.py::test_rand", project_dir=tmp_path)
    res_rand = disambiguate_causes(ambiguous_cause, info_rand)
    assert res_rand["disambiguated"] is True
    assert res_rand["resolved_candidates"] == ["unseeded_randomness"]
    assert res_rand["fault_side"] == "Code Side"

    info_race = inspect_test_source("tests/test_worker.py::test_race_unjoined", project_dir=tmp_path)
    res_race = disambiguate_causes(ambiguous_cause, info_race)
    assert res_race["disambiguated"] is True
    assert res_race["resolved_candidates"] == ["race_conditions"]
    assert res_race["fault_side"] == "Code Side"

    # Negative test: synchronized joined single thread + Lock + Event must NOT narrow to race_conditions
    info_sync = inspect_test_source("tests/test_worker.py::test_synchronized_joined", project_dir=tmp_path)
    res_sync = disambiguate_causes(ambiguous_cause, info_sync)
    assert res_sync["disambiguated"] is False
    assert res_sync["resolved_candidates"] == ["unseeded_randomness", "race_conditions"]
    assert res_sync["fault_side"] == "Undetermined"
    assert res_sync["basis"] == "Ambiguous"

    hash_cause = [
        {
            "candidates": ["hash_order_sensitivity"],
            "status": "suspected",
            "reason": "Passed in repeat, failed in hashseed.",
            "run_ids": [1, 3],
        }
    ]
    info_hash = inspect_test_source("tests/test_worker.py::test_hash", project_dir=tmp_path)
    res_hash = disambiguate_causes(hash_cause, info_hash)
    assert res_hash["resolved_candidates"] == ["hash_order_sensitivity"]
    assert res_hash["fault_side"] == "Test Side"

    shared_cause = [
        {
            "candidates": ["shared_state_interference"],
            "status": "suspected",
            "reason": "Failed in order, passed in isolated.",
            "run_ids": [4, 5],
        }
    ]
    info_shared = inspect_test_source("tests/test_worker.py::test_shared", project_dir=tmp_path)
    res_shared = disambiguate_causes(shared_cause, info_shared)
    assert res_shared["resolved_candidates"] == ["shared_state_interference"]
    assert res_shared["fault_side"] == "Test Side"


def test_real_scenario_all_18_tests_correctly_classified():
    """Verify AST heuristics against the actual 18-test scenario in scenario/."""
    scenario_dir = Path(__file__).resolve().parents[1] / "scenario"
    ambiguous = [
        {
            "candidates": ["unseeded_randomness", "race_conditions"],
            "status": "ambiguous",
            "reason": "Varying outcomes in repeat.",
            "run_ids": [1, 2],
        }
    ]
    for name in (
        "test_default_avatar_assignment",
        "test_select_promo_banner",
        "test_assign_support_agent",
        "test_get_daily_quote",
    ):
        info = inspect_test_source(f"tests/test_cart.py::{name}", project_dir=scenario_dir)
        res = disambiguate_causes(ambiguous, info)
        assert res["resolved_candidates"] == ["unseeded_randomness"], name
        assert res["fault_side"] == "Code Side", name

    for name in (
        "test_checkout_deducts_inventory",
        "test_checkout_deducts_mouse",
        "test_checkout_deducts_keyboard",
        "test_checkout_deducts_both",
    ):
        info = inspect_test_source(f"tests/test_cart.py::{name}", project_dir=scenario_dir)
        res = disambiguate_causes(ambiguous, info)
        assert res["resolved_candidates"] == ["race_conditions"], name
        assert res["fault_side"] == "Code Side", name

    hash_cause = [
        {
            "candidates": ["hash_order_sensitivity"],
            "status": "suspected",
            "reason": "Passed in repeat, failed in hashseed.",
            "run_ids": [1, 11],
        }
    ]
    for name in (
        "test_applied_discounts_format",
        "test_preferences_format",
        "test_metadata_format",
        "test_taxes_format",
    ):
        info = inspect_test_source(f"tests/test_cart.py::{name}", project_dir=scenario_dir)
        res = disambiguate_causes(hash_cause, info)
        assert res["resolved_candidates"] == ["hash_order_sensitivity"], name
        assert res["fault_side"] == "Test Side", name

    shared_cause = [
        {
            "candidates": ["shared_state_interference"],
            "status": "suspected",
            "reason": "Flipped under shuffled order, passed in isolated.",
            "run_ids": [19, 25],
        }
    ]
    for name in (
        "test_new_cart_is_empty",
        "test_shared_cart_has_no_prior_laptop",
        "test_single_item_in_shared_cart",
        "test_shared_cart_only_contains_added_monitor",
    ):
        info = inspect_test_source(f"tests/test_cart.py::{name}", project_dir=scenario_dir)
        res = disambiguate_causes(shared_cause, info)
        assert res["resolved_candidates"] == ["shared_state_interference"], name
        assert res["fault_side"] == "Test Side", name

    tz_cause = [
        {
            "candidates": ["timezone_sensitivity"],
            "status": "suspected",
            "reason": "Outcomes differ across timezones (TZ) while repeat outcomes are consistent.",
            "run_ids": [1, 25],
        }
    ]
    for name in (
        "test_daily_order_prefix_uses_utc_date",
        "test_coupon_expiry_date_check",
    ):
        info = inspect_test_source(f"tests/test_cart.py::{name}", project_dir=scenario_dir)
        res = disambiguate_causes(tz_cause, info)
        assert res["resolved_candidates"] == ["timezone_sensitivity"], name
        assert res["fault_side"] == "Code Side", name

    for name in (
        "test_receipt_timestamp_formatting",
        "test_audit_log_date_tag",
    ):
        info = inspect_test_source(f"tests/test_cart.py::{name}", project_dir=scenario_dir)
        res = disambiguate_causes(tz_cause, info)
        assert res["resolved_candidates"] == ["timezone_sensitivity"], name
        assert res["fault_side"] == "Test Side", name


def test_timezone_ast_heuristics_and_suppression(tmp_path):
    """Verify AST detection and false-positive suppression for timezone_sensitivity."""
    src_dir = tmp_path / "src" / "billing"
    src_dir.mkdir(parents=True)
    (src_dir / "__init__.py").write_text("", encoding="utf-8")
    (src_dir / "clock.py").write_text(
        "from datetime import date, datetime, timezone\n"
        "import time\n\n"
        "class InvoiceClock:\n"
        "    def naive_stamp(self):\n"
        "        return datetime.now().strftime('%Y-%m-%d')\n\n"
        "    def explicit_utc_stamp(self):\n"
        "        return datetime.now(timezone.utc).strftime('%Y-%m-%d')\n\n"
        "    def gmtime_stamp(self, ts):\n"
        "        return time.strftime('%Y-%m-%d', time.gmtime(ts))\n",
        encoding="utf-8",
    )

    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_clock.py").write_text(
        "from datetime import datetime, timezone\n"
        "import time\n"
        "from billing.clock import InvoiceClock\n\n"
        "def test_code_side_tz():\n"
        "    c = InvoiceClock()\n"
        "    assert c.naive_stamp() == datetime.now(timezone.utc).strftime('%Y-%m-%d')\n\n"
        "def test_test_side_tz():\n"
        "    c = InvoiceClock()\n"
        "    expected = datetime.fromtimestamp(1704067200).strftime('%Y-%m-%d')\n"
        "    assert c.explicit_utc_stamp() == expected\n\n"
        "def test_explicit_tz_clean():\n"
        "    c = InvoiceClock()\n"
        "    ts = datetime.fromtimestamp(1704067200, tz=timezone.utc).strftime('%Y-%m-%d')\n"
        "    assert c.gmtime_stamp(1704067200) == ts\n\n"
        "def test_frozen_time_suppressed(freezer):\n"
        "    now_str = datetime.now().strftime('%Y-%m-%d')\n"
        "    assert len(now_str) == 10\n",
        encoding="utf-8",
    )

    tz_cause = [
        {
            "candidates": ["timezone_sensitivity"],
            "status": "suspected",
            "reason": "Outcomes differ across timezones (TZ) while repeat outcomes are consistent.",
            "run_ids": [1, 2],
        }
    ]

    info_code = inspect_test_source("tests/test_clock.py::test_code_side_tz", project_dir=tmp_path)
    res_code = disambiguate_causes(tz_cause, info_code)
    assert res_code["fault_side"] == "Code Side"
    assert res_code["basis"] == "Runtime+AST"

    info_test = inspect_test_source("tests/test_clock.py::test_test_side_tz", project_dir=tmp_path)
    res_test = disambiguate_causes(tz_cause, info_test)
    assert res_test["fault_side"] == "Test Side"
    assert res_test["basis"] == "Runtime+AST"

    info_clean = inspect_test_source("tests/test_clock.py::test_explicit_tz_clean", project_dir=tmp_path)
    assert info_clean["indicators"]["timezone_sensitivity"] == []

    info_frozen = inspect_test_source("tests/test_clock.py::test_frozen_time_suppressed", project_dir=tmp_path)
    assert info_frozen["indicators"]["timezone_sensitivity"] == []

    diag = _sample_diagnosis()
    diag["tests"][1]["test_id"] = "tests/test_clock.py::test_code_side_tz"
    diag["tests"][1]["suspected_causes"] = tz_cause
    md_en = render_markdown(diag, project_dir=tmp_path, lang="en")
    assert "`timezone_sensitivity`" in md_en
    assert "without explicit timezone (`tz`) or time freeze" in md_en

    md_zh = render_markdown(diag, project_dir=tmp_path, lang="zh")
    assert "时区/本地时间敏感" in md_zh
    assert "且未显式指定时区（`tz`）或固定时间" in md_zh


def test_parametrized_and_missing_source_degrade_gracefully(tmp_path):
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_param.py").write_text(
        "import random\n"
        "class TestSuite:\n"
        "    def test_case(self):\n"
        "        assert random.randint(0, 1) == 0\n",
        encoding="utf-8",
    )
    info = inspect_test_source(
        "tests/test_param.py::TestSuite::test_case[a::b-1]",
        project_dir=tmp_path,
    )
    assert info["available"] is True
    assert info["indicators"]["unseeded_randomness"][0]["side"] == "Test Side"

    missing = inspect_test_source("tests/nonexistent.py::test_none", project_dir=tmp_path)
    assert missing["available"] is False


def test_edge_cases_special_test_ids_multiple_causes_and_abnormal_runs():
    """Cover parametrized IDs with pipes/backticks/::, multiple causes, consistently_failing, and abnormal runs."""
    diag = {
        "schema_version": 1,
        "policy": {
            "stability_score": "passed / (passed + failed + error)",
            "minimum_valid_runs": 2,
            "scope": "Observed executions only.",
        },
        "summary": {
            "tests": 3,
            "runs": 3,
            "records": 6,
            "verdicts": {
                "stable": 0,
                "consistently_failing": 1,
                "flaky": 1,
                "inconclusive": 1,
            },
        },
        "runs": [
            {
                "run_id": 1,
                "condition": "repeat",
                "condition_value": "1",
                "hash_seed": 0,
                "exit_code": 1,
                "status": "ok",
                "seconds": 0.1,
                "tests_expected": 3,
                "tests_reported": 3,
                "log_path": "logs/run_001.log",
            },
            {
                "run_id": 2,
                "condition": "hashseed",
                "condition_value": "101",
                "hash_seed": 101,
                "exit_code": 1,
                "status": "timeout",
                "seconds": None,
                "tests_expected": 3,
                "tests_reported": 2,
                "log_path": "logs/run_002.log",
            },
            {
                "run_id": 3,
                "condition": "order",
                "condition_value": "202",
                "hash_seed": 0,
                "exit_code": 0,
                "status": "ok",
                "seconds": 0.2,
                "tests_expected": 3,
                "tests_reported": 2,
                "log_path": "logs/run_003.log",
            },
        ],
        "tests": [
            {
                "test_id": 'tests/test_edge.py::TestSuite::test_param[a::b, "c" | `d`]',
                "overall": _summary_block(passed=1, failed=1, verdict="flaky"),
                "conditions": {
                    "repeat": _summary_block(passed=1, failed=0, verdict="inconclusive"),
                    "hashseed": _summary_block(passed=0, failed=1, verdict="inconclusive"),
                    "order": _summary_block(passed=0, failed=0, verdict="inconclusive"),
                    "isolated": _summary_block(passed=0, failed=0, verdict="inconclusive"),
                },
                "suspected_causes": [
                    {
                        "candidates": ["hash_order_sensitivity"],
                        "status": "suspected",
                        "reason": "Varied under hashseed 101.",
                        "run_ids": [1, 2],
                    },
                    {
                        "candidates": ["shared_state_interference"],
                        "status": "suspected",
                        "reason": "Varied under order 202.",
                        "run_ids": [1, 3],
                    },
                ],
                "warnings": ["Warning one for param test.", "Warning two for param test."],
                "evidence": [
                    {"run_id": 1, "position": 1, "outcome": "passed"},
                    {"run_id": 2, "position": 1, "outcome": "failed"},
                ],
            },
            {
                "test_id": "tests/test_edge.py::test_always_broken",
                "overall": _summary_block(passed=0, failed=1, error=1, verdict="consistently_failing"),
                "conditions": {
                    "repeat": _summary_block(passed=0, failed=1, verdict="inconclusive"),
                    "hashseed": _summary_block(passed=0, error=1, verdict="inconclusive"),
                    "order": _summary_block(passed=0, failed=0, verdict="inconclusive"),
                    "isolated": _summary_block(passed=0, failed=0, verdict="inconclusive"),
                },
                "suspected_causes": [],
                "warnings": ["Always failed or errored."],
                "evidence": [
                    {"run_id": 1, "position": 2, "outcome": "failed"},
                    {"run_id": 2, "position": 2, "outcome": "error"},
                ],
            },
            {
                "test_id": "tests/test_edge.py::test_skipped_inconclusive",
                "overall": _summary_block(passed=0, failed=0, skipped=1, timeout=1, verdict="inconclusive"),
                "conditions": {
                    "repeat": _summary_block(passed=0, skipped=1, verdict="inconclusive"),
                    "hashseed": _summary_block(passed=0, timeout=1, verdict="inconclusive"),
                    "order": _summary_block(passed=0, verdict="inconclusive"),
                    "isolated": _summary_block(passed=0, verdict="inconclusive"),
                },
                "suspected_causes": [],
                "warnings": ["Insufficient valid runs (0 < 2)."],
                "evidence": [
                    {"run_id": 1, "position": 3, "outcome": "skipped"},
                    {"run_id": 3, "position": None, "outcome": "timeout"},
                ],
            },
        ],
    }

    md = render_markdown(diag, project_dir=None)
    # 2 abnormal/incomplete runs (run 2 has status=timeout & 2<3; run 3 has 2<3)
    assert "- **Abnormal / Incomplete Runs:** 2" in md
    # Full parametrized name with embedded :: and escaped pipe/backticks preserved
    assert 'test_param[a::b, "c" \\| `d`]' in md
    # Both suspected causes and their distinct reasons/runs appear
    assert "Varied under hashseed 101." in md
    assert "Varied under order 202." in md
    assert "`hash_order_sensitivity`: run 1 (`logs/run_001.log`), run 2 (`logs/run_002.log`)" in md
    assert "`shared_state_interference`: run 1 (`logs/run_001.log`), run 3 (`logs/run_003.log`)" in md
    # Consistently failing and inconclusive sections and multiple warnings appear
    assert "`consistently_failing`" in md
    assert "0.50 / 0.50" in md
    assert "N/A / N/A" in md
    assert "Warning one for param test." in md
    assert "Warning two for param test." in md
    assert "Always failed or errored." in md


def test_render_markdown_handles_nullable_scores_warnings_and_no_project():
    diag = _sample_diagnosis()
    diag["tests"][0]["overall"] = _summary_block(passed=0, failed=0, skipped=2, verdict="inconclusive")
    diag["tests"][0]["conditions"]["repeat"] = _summary_block(
        passed=0, failed=0, skipped=2, verdict="inconclusive"
    )
    diag["tests"][0]["warnings"] = ["Insufficient valid runs (0 < 2)."]
    diag["summary"]["verdicts"]["stable"] = 0
    diag["summary"]["verdicts"]["inconclusive"] = 1
    md = render_markdown(diag, project_dir=None)
    assert "# Test-Stability Triage Report" in md
    assert "N/A" in md
    assert "Insufficient valid runs (0 < 2)." in md
    assert "heuristic hints" in md


def test_supplied_analysis_fixture_and_duplicate_short_names():
    """Verify report generation on the 21-test analysis fixture and disambiguated duplicate leaf names."""
    from analysis import analyse, load_records

    fixture_dir = Path(__file__).resolve().parent / "fixtures" / "analysis"
    diagnosis = analyse(*load_records(fixture_dir / "results.csv", fixture_dir / "run_meta.csv"))
    md = render_markdown(diagnosis, project_dir=None)
    assert "- **Tests Analysed:** 21" in md
    assert "`flaky`: 8 | `stable`: 9 | `consistently_failing`: 4 | `inconclusive`: 0" in md

    # Duplicate leaf names across different modules must render with full test_id
    dup_diag = _sample_diagnosis()
    dup_diag["tests"][0]["test_id"] = "tests/mod_a/test_suite.py::test_same_leaf"
    dup_diag["tests"][1]["test_id"] = "tests/mod_b/test_suite.py::test_same_leaf"
    md_dup = render_markdown(dup_diag, project_dir=None)
    assert "`tests/mod_a/test_suite.py::test_same_leaf`" in md_dup
    assert "`tests/mod_b/test_suite.py::test_same_leaf`" in md_dup


def test_cli_exit_codes_and_atomic_write(tmp_path):
    diag = _sample_diagnosis()
    diag_file = tmp_path / "diagnosis.json"
    diag_file.write_text(json.dumps(diag), encoding="utf-8")
    out_dir = tmp_path / "out"

    # Exit code 0 on valid input
    assert main([str(diag_file), str(out_dir)]) == 0
    report_path = out_dir / "report.md"
    assert report_path.is_file()
    original_text = report_path.read_text(encoding="utf-8")
    assert "test_random_flaky" in original_text

    # Exit code 3 on missing input file
    assert main([str(tmp_path / "missing.json"), str(out_dir)]) == 3
    assert report_path.read_text(encoding="utf-8") == original_text

    # Exit code 4 on invalid JSON syntax, preserving existing report.md
    bad_file = tmp_path / "bad.json"
    bad_file.write_text("{not valid json", encoding="utf-8")
    assert main([str(bad_file), str(out_dir)]) == 4
    assert report_path.read_text(encoding="utf-8") == original_text

    # Exit code 4 on structurally valid JSON with inconsistent score/verdict, preserving report.md
    inconsistent_diag = copy.deepcopy(diag)
    inconsistent_diag["tests"][1]["overall"]["stability_score"] = 1.0
    inconsistent_diag["tests"][1]["overall"]["verdict"] = "stable"
    inconsistent_file = tmp_path / "inconsistent.json"
    inconsistent_file.write_text(json.dumps(inconsistent_diag), encoding="utf-8")
    assert main([str(inconsistent_file), str(out_dir)]) == 4
    assert report_path.read_text(encoding="utf-8") == original_text

    # Exit code 5 when output_dir is a regular file instead of a directory
    blocked_out = tmp_path / "blocked_out"
    blocked_out.write_text("file blocking mkdir", encoding="utf-8")
    assert main([str(diag_file), str(blocked_out)]) == 5

    # Exit code 2 via subprocess when arguments are missing
    proc = subprocess.run(
        [sys.executable, "-m", "report"],
        env={"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2


def test_extended_ast_heuristics_fixtures_code_side_shared_and_seed_suppression(tmp_path):
    """Verify conftest fixtures, setup_method self.<attr>, Code Side shared state, and seed/sorted exclusions."""
    src_dir = tmp_path / "src" / "app"
    src_dir.mkdir(parents=True)
    (src_dir / "__init__.py").write_text("", encoding="utf-8")
    (src_dir / "cache_mod.py").write_text(
        "from functools import lru_cache\n"
        "import uuid\n"
        "from concurrent.futures import ThreadPoolExecutor\n\n"
        "_GLOBAL_CACHE = []\n\n"
        "class Engine:\n"
        "    def mutate_global(self, item):\n"
        "        _GLOBAL_CACHE.append(item)\n"
        "        return len(_GLOBAL_CACHE)\n\n"
        "    @lru_cache(maxsize=16)\n"
        "    def cached_lookup(self, key):\n"
        "        return key\n\n"
        "    def make_id(self):\n"
        "        return str(uuid.uuid4())\n\n"
        "    def run_pool(self):\n"
        "        with ThreadPoolExecutor(max_workers=2) as ex:\n"
        "            ex.submit( lambda: 1 )\n",
        encoding="utf-8",
    )

    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "conftest.py").write_text(
        "import pytest\n"
        "from app.cache_mod import Engine\n\n"
        "@pytest.fixture\n"
        "def engine_fix():\n"
        "    eng = Engine()\n"
        "    yield eng\n",
        encoding="utf-8",
    )
    (tests_dir / "test_ext.py").write_text(
        "import os\n"
        "import random\n"
        "from app.cache_mod import Engine\n\n"
        "def test_via_fixture_code_shared(engine_fix):\n"
        "    assert engine_fix.mutate_global('x') == 1\n\n"
        "def test_seeded_random_and_sorted_join():\n"
        "    random.seed(42)\n"
        "    val = random.randint(1, 10)\n"
        "    p = os.path.join('a', 'b')\n"
        "    s = ','.join(sorted(['b', 'a']))\n"
        "    assert val > 0 and p and s == 'a,b'\n\n"
        "class TestWithSetup:\n"
        "    def setup_method(self):\n"
        "        self.eng = Engine()\n\n"
        "    def test_uuid_via_self(self):\n"
        "        assert len(self.eng.make_id()) > 0\n\n"
        "    def test_pool_via_self(self):\n"
        "        self.eng.run_pool()\n",
        encoding="utf-8",
    )

    # 1. Fixture + Code Side shared_state_interference
    info_fix = inspect_test_source(
        "tests/test_ext.py::test_via_fixture_code_shared", project_dir=tmp_path
    )
    assert info_fix["available"] is True
    assert any(h["side"] == "Code Side" for h in info_fix["indicators"]["shared_state_interference"])

    # 2. Seeded random + os.path.join + sorted() join must NOT trigger false-positive indicators
    info_clean = inspect_test_source(
        "tests/test_ext.py::test_seeded_random_and_sorted_join", project_dir=tmp_path
    )
    assert info_clean["indicators"]["unseeded_randomness"] == []
    assert info_clean["indicators"]["hash_order_sensitivity"] == []

    # 3. setup_method self.eng -> uuid.uuid4() (Code Side unseeded_randomness) & ThreadPoolExecutor (race_conditions)
    info_uuid = inspect_test_source(
        "tests/test_ext.py::TestWithSetup::test_uuid_via_self", project_dir=tmp_path
    )
    assert info_uuid["indicators"]["unseeded_randomness"][0]["side"] == "Code Side"

    info_pool = inspect_test_source(
        "tests/test_ext.py::TestWithSetup::test_pool_via_self", project_dir=tmp_path
    )
    assert info_pool["indicators"]["race_conditions"][0]["side"] == "Code Side"


def test_chinese_report_rendering_and_cli(tmp_path):
    """Verify --lang zh generates a localized Chinese Markdown report with deduplicated headings,
    translated conflict hints, and East-Asian-width-aligned Markdown tables."""
    import unicodedata

    def disp_width(text):
        return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)

    scenario_dir = Path(__file__).resolve().parents[1] / "scenario"
    diagnosis = _sample_diagnosis()
    diagnosis["tests"][1]["test_id"] = "tests/test_cart.py::test_get_daily_quote"
    second_rand = copy.deepcopy(diagnosis["tests"][1])
    second_rand["test_id"] = "tests/test_cart.py::test_select_promo_banner"
    conflict_test = copy.deepcopy(diagnosis["tests"][1])
    conflict_test["test_id"] = "tests/test_cart.py::test_default_avatar_assignment"
    conflict_test["suspected_causes"] = [
        {
            "candidates": ["hash_order_sensitivity"],
            "status": "suspected",
            "reason": "Outcomes differ across hash seeds while repeat outcomes are consistent.",
            "run_ids": [1, 2],
        }
    ]
    diagnosis["tests"].extend([second_rand, conflict_test])
    diagnosis["summary"]["tests"] = len(diagnosis["tests"])
    diagnosis["summary"]["records"] = sum(len(t["evidence"]) for t in diagnosis["tests"])
    diagnosis["summary"]["verdicts"]["flaky"] = 3

    md_zh = render_markdown(diagnosis, project_dir=scenario_dir, lang="zh")
    assert "# 测试稳定性诊断报告 (Test-Stability Triage Report)" in md_zh
    assert "## 1. 概览摘要" in md_zh
    assert "## 2. 测试诊断汇总表" in md_zh
    assert "## 3. 诊断详情与证据溯源" in md_zh
    assert "未固定随机种子" in md_zh
    assert "业务侧 (Code Side)" in md_zh
    assert "AST 消歧 *" in md_zh
    # Heading fault side must NOT repeat when multiple tests share the same fault side
    assert "业务侧 (Code Side) / 业务侧 (Code Side)" not in md_zh
    # Conflicting AST hint and basis must be translated into Chinese
    assert "运行时 (AST 冲突)" in md_zh
    assert "冲突的 AST 提示：" in md_zh
    assert "但运行时证据指向" in md_zh
    assert "Conflicting hint:" not in md_zh

    # Every Markdown table block must have visually aligned pipes under East Asian display width
    table_lines = [ln for ln in md_zh.splitlines() if ln.startswith("| ")]
    assert table_lines
    # Group consecutive table lines and assert uniform display width per table
    current_table = []
    for ln in md_zh.splitlines():
        if ln.startswith("| "):
            current_table.append(ln)
        elif current_table:
            widths = {disp_width(row) for row in current_table}
            assert len(widths) == 1, f"Misaligned Chinese table rows: {current_table}"
            current_table = []
    if current_table:
        widths = {disp_width(row) for row in current_table}
        assert len(widths) == 1, f"Misaligned Chinese table rows: {current_table}"

    diag_path = tmp_path / "diagnosis.json"
    out_dir = tmp_path / "out_zh"
    diag_path.write_text(json.dumps(diagnosis), encoding="utf-8")
    rc = main([str(diag_path), str(out_dir), "--project", str(scenario_dir), "--lang", "zh"])
    assert rc == 0
    written = (out_dir / "report.md").read_text(encoding="utf-8")
    assert "## 2. 测试诊断汇总表" in written


def test_resource_leak_ast_heuristics_disambiguation_and_suppression(tmp_path):
    """Verify AST detection, cross-file tracking, disambiguation, and tmp_path/port=0 suppression for resource_leak."""
    src_dir = tmp_path / "src" / "storage"
    src_dir.mkdir(parents=True)
    (src_dir / "__init__.py").write_text("", encoding="utf-8")
    (src_dir / "repo.py").write_text(
        "from pathlib import Path\n"
        "import socket\n"
        "import sqlite3\n\n"
        "class DataStore:\n"
        "    def write_fixed_cache(self, payload):\n"
        "        Path('cache.db').write_text(payload)\n"
        "        conn = sqlite3.connect('local_state.sqlite')\n"
        "        conn.close()\n\n"
        "    def bind_fixed_port(self):\n"
        "        s = socket.socket()\n"
        "        s.bind(('127.0.0.1', 8080))\n"
        "        return s\n\n"
        "    def bind_ephemeral_port(self):\n"
        "        s = socket.socket()\n"
        "        s.bind(('127.0.0.1', 0))\n"
        "        return s\n\n"
        "    def memory_sqlite(self):\n"
        "        return sqlite3.connect(':memory:')\n",
        encoding="utf-8",
    )

    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_resource.py").write_text(
        "import tempfile\n"
        "from pathlib import Path\n"
        "from storage.repo import DataStore\n\n"
        "def test_test_side_hardcoded_open():\n"
        "    with open('temp_output.json', 'w') as f:\n"
        "        f.write('{}')\n"
        "    assert Path('temp_output.json').exists()\n\n"
        "def test_code_side_file_and_sqlite_leak():\n"
        "    store = DataStore()\n"
        "    store.write_fixed_cache('hello')\n\n"
        "def test_code_side_fixed_port_leak():\n"
        "    store = DataStore()\n"
        "    sock = store.bind_fixed_port()\n"
        "    sock.close()\n\n"
        "def test_suppressed_with_tmp_path(tmp_path):\n"
        "    out = tmp_path / 'temp.json'\n"
        "    out.write_text('ok')\n"
        "    with open(out, 'w') as f:\n"
        "        f.write('ok')\n\n"
        "def test_suppressed_ephemeral_port_and_tempfile():\n"
        "    store = DataStore()\n"
        "    s = store.bind_ephemeral_port()\n"
        "    conn = store.memory_sqlite()\n"
        "    with tempfile.NamedTemporaryFile('w') as tf:\n"
        "        tf.write('clean')\n"
        "    s.close()\n"
        "    conn.close()\n",
        encoding="utf-8",
    )

    shared_cause = [
        {
            "candidates": ["shared_state_interference"],
            "status": "suspected",
            "reason": "Outcomes differ across test orders, or isolated executions pass while suite executions fail, with a consistent repeat baseline.",
            "run_ids": [1, 2],
        }
    ]
    ambiguous_cause = [
        {
            "candidates": ["unseeded_randomness", "race_conditions"],
            "status": "ambiguous",
            "reason": "Outcomes vary under the same recorded seed and collected order; source investigation is needed to distinguish these candidates.",
            "run_ids": [1, 2],
        }
    ]

    info_test_file = inspect_test_source(
        "tests/test_resource.py::test_test_side_hardcoded_open", project_dir=tmp_path
    )
    res_test_file = disambiguate_causes(shared_cause, info_test_file)
    assert res_test_file["resolved_candidates"] == ["resource_leak"]
    assert res_test_file["runtime_candidates"] == ["shared_state_interference"]
    assert res_test_file["resolution"] == "refined"
    assert res_test_file["refined"] is True
    assert res_test_file["disambiguated"] is False
    assert res_test_file["basis"] == "AST Refined *"
    assert shared_cause[0]["candidates"] == ["resource_leak"]
    assert shared_cause[0]["runtime_candidates"] == ["shared_state_interference"]
    assert shared_cause[0]["runtime_status"] == "suspected"
    assert shared_cause[0]["resolution"] == "refined"
    assert (
        shared_cause[0]["reason"]
        == "Outcomes vary across runs or test orders due to unisolated file or port resources."
    )
    assert res_test_file["fault_side"] == "Test Side"
    assert res_test_file["conflicting_hints"] == []

    info_code_file = inspect_test_source(
        "tests/test_resource.py::test_code_side_file_and_sqlite_leak", project_dir=tmp_path
    )
    res_code_file = disambiguate_causes(shared_cause, info_code_file)
    assert res_code_file["resolved_candidates"] == ["resource_leak"]
    assert res_code_file["runtime_candidates"] == ["shared_state_interference"]
    assert res_code_file["resolution"] == "refined"
    assert res_code_file["refined"] is True
    assert res_code_file["disambiguated"] is False
    assert res_code_file["fault_side"] == "Code Side"

    info_code_port = inspect_test_source(
        "tests/test_resource.py::test_code_side_fixed_port_leak", project_dir=tmp_path
    )
    res_code_port = disambiguate_causes(ambiguous_cause, info_code_port)
    assert res_code_port["resolved_candidates"] == ["resource_leak"]
    assert res_code_port["runtime_candidates"] == ["unseeded_randomness", "race_conditions"]
    assert res_code_port["resolution"] == "disambiguated"
    assert res_code_port["disambiguated"] is True
    assert res_code_port["refined"] is False
    assert ambiguous_cause[0]["candidates"] == ["resource_leak"]
    assert ambiguous_cause[0]["runtime_candidates"] == ["unseeded_randomness", "race_conditions"]
    assert ambiguous_cause[0]["runtime_status"] == "ambiguous"
    assert (
        ambiguous_cause[0]["reason"]
        == "Outcomes vary across runs or test orders due to unisolated file or port resources."
    )
    assert res_code_port["fault_side"] == "Code Side"

    info_tmp_clean = inspect_test_source(
        "tests/test_resource.py::test_suppressed_with_tmp_path", project_dir=tmp_path
    )
    assert info_tmp_clean["indicators"]["resource_leak"] == []

    info_port0_clean = inspect_test_source(
        "tests/test_resource.py::test_suppressed_ephemeral_port_and_tempfile", project_dir=tmp_path
    )
    assert info_port0_clean["indicators"]["resource_leak"] == []

    diag = _sample_diagnosis()
    diag["tests"][1]["test_id"] = "tests/test_resource.py::test_code_side_file_and_sqlite_leak"
    diag["tests"][1]["suspected_causes"] = shared_cause
    md_zh = render_markdown(diag, project_dir=tmp_path, lang="zh")
    assert "文件与端口资源冲突" in md_zh
    assert "通过以下调用操作了硬编码的文件/数据库路径：" in md_zh
    assert "operates on hardcoded" not in md_zh


def test_external_io_ast_heuristics_disambiguation_and_mock_suppression(tmp_path):
    """Verify AST detection, 3-hop Code Side attribution, and monkeypatch/@patch/responses suppression for external_io."""
    src_dir = tmp_path / "src" / "net"
    src_dir.mkdir(parents=True)
    (src_dir / "__init__.py").write_text("", encoding="utf-8")
    (src_dir / "client.py").write_text(
        "import requests\n"
        "import urllib.request\n\n"
        "def _low_level_fetch(url):\n"
        "    return requests.get(url, timeout=2)\n\n"
        "class Gateway:\n"
        "    def fetch_profile(self, uid):\n"
        "        return _low_level_fetch(f'https://api.example.com/u/{uid}')\n\n"
        "    def open_feed(self):\n"
        "        return urllib.request.urlopen('https://example.com/feed.xml')\n",
        encoding="utf-8",
    )

    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_net.py").write_text(
        "from unittest.mock import patch\n"
        "import httpx\n"
        "import responses\n"
        "from net.client import Gateway\n\n"
        "def test_test_side_httpx():\n"
        "    resp = httpx.post('https://example.com/hook')\n"
        "    assert resp.status_code == 200\n\n"
        "def test_code_side_unmocked_requests():\n"
        "    gw = Gateway()\n"
        "    assert gw.fetch_profile(1) is not None\n\n"
        "def test_suppressed_with_monkeypatch(monkeypatch):\n"
        "    gw = Gateway()\n"
        "    assert gw.fetch_profile(1) is not None\n\n"
        "@patch('net.client.requests.get')\n"
        "def test_suppressed_with_patch_decorator(mock_get):\n"
        "    gw = Gateway()\n"
        "    gw.fetch_profile(1)\n\n"
        "@responses.activate\n"
        "def test_suppressed_with_responses_decorator():\n"
        "    gw = Gateway()\n"
        "    gw.fetch_profile(1)\n",
        encoding="utf-8",
    )

    ambiguous_cause = [
        {
            "candidates": ["unseeded_randomness", "race_conditions"],
            "status": "ambiguous",
            "reason": "Outcomes vary under the same recorded seed and collected order; source investigation is needed to distinguish these candidates.",
            "run_ids": [1, 2],
        }
    ]

    info_test_io = inspect_test_source(
        "tests/test_net.py::test_test_side_httpx", project_dir=tmp_path
    )
    res_test_io = disambiguate_causes(ambiguous_cause, info_test_io)
    assert res_test_io["resolved_candidates"] == ["external_io"]
    assert res_test_io["disambiguated"] is True
    assert res_test_io["fault_side"] == "Test Side"

    info_code_io = inspect_test_source(
        "tests/test_net.py::test_code_side_unmocked_requests", project_dir=tmp_path
    )
    res_code_io = disambiguate_causes(ambiguous_cause, info_code_io)
    assert res_code_io["resolved_candidates"] == ["external_io"]
    assert res_code_io["disambiguated"] is True
    assert res_code_io["fault_side"] == "Code Side"

    for clean_name in (
        "test_suppressed_with_monkeypatch",
        "test_suppressed_with_patch_decorator",
        "test_suppressed_with_responses_decorator",
    ):
        info_clean = inspect_test_source(f"tests/test_net.py::{clean_name}", project_dir=tmp_path)
        assert info_clean["indicators"]["external_io"] == [], clean_name

    diag = _sample_diagnosis()
    diag["tests"][1]["test_id"] = "tests/test_net.py::test_code_side_unmocked_requests"
    diag["tests"][1]["suspected_causes"] = ambiguous_cause
    md_zh = render_markdown(diag, project_dir=tmp_path, lang="zh")
    assert "未隔离的外部网络依赖" in md_zh
    assert "通过以下调用发起了未 Mock 拦截的外部网络请求：" in md_zh
    assert "performs unmocked" not in md_zh


def test_float_precision_ast_heuristics_disambiguation_and_approx_suppression(tmp_path):
    """Verify AST detection, cross-file float return tracking, and pytest.approx/math.isclose/round suppression."""
    src_dir = tmp_path / "src" / "pricing"
    src_dir.mkdir(parents=True)
    (src_dir / "__init__.py").write_text("", encoding="utf-8")
    (src_dir / "calc.py").write_text(
        "class TaxCalculator:\n"
        "    def compute_taxed_total(self, amounts):\n"
        "        return sum(amounts) * 1.07\n",
        encoding="utf-8",
    )

    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_float.py").write_text(
        "import math\n"
        "from decimal import Decimal\n"
        "import pytest\n"
        "from pricing.calc import TaxCalculator\n\n"
        "def test_direct_float_literal_assert():\n"
        "    total = 0.1 + 0.2\n"
        "    assert total == 0.3\n\n"
        "def test_cross_file_float_return_assert(expected_val):\n"
        "    calc = TaxCalculator()\n"
        "    res = calc.compute_taxed_total([10.1, 20.2])\n"
        "    assert res == expected_val\n\n"
        "def test_suppressed_with_pytest_approx():\n"
        "    total = 0.1 + 0.2\n"
        "    assert total == pytest.approx(0.3)\n\n"
        "def test_suppressed_with_math_isclose_and_round():\n"
        "    val = sum([0.1, 0.2])\n"
        "    assert math.isclose(val, 0.3)\n"
        "    assert round(val, 2) == 0.3\n"
        "    assert Decimal('0.1') + Decimal('0.2') == Decimal('0.3')\n",
        encoding="utf-8",
    )

    hash_cause = [
        {
            "candidates": ["hash_order_sensitivity"],
            "status": "suspected",
            "reason": "Outcomes differ across hash seeds while repeat outcomes are consistent.",
            "run_ids": [1, 2],
        }
    ]

    info_direct = inspect_test_source(
        "tests/test_float.py::test_direct_float_literal_assert", project_dir=tmp_path
    )
    res_direct = disambiguate_causes(hash_cause, info_direct)
    assert res_direct["resolved_candidates"] == ["float_precision"]
    assert res_direct["runtime_candidates"] == ["hash_order_sensitivity"]
    assert res_direct["resolution"] == "refined"
    assert res_direct["refined"] is True
    assert res_direct["disambiguated"] is False
    assert res_direct["basis"] == "AST Refined *"
    assert hash_cause[0]["candidates"] == ["float_precision"]
    assert hash_cause[0]["runtime_candidates"] == ["hash_order_sensitivity"]
    assert hash_cause[0]["runtime_status"] == "suspected"
    assert (
        hash_cause[0]["reason"]
        == "Outcomes vary across hash seeds or repeated runs due to strict floating-point equality assertions."
    )
    assert res_direct["fault_side"] == "Test Side"
    assert res_direct["conflicting_hints"] == []

    info_cross = inspect_test_source(
        "tests/test_float.py::test_cross_file_float_return_assert", project_dir=tmp_path
    )
    res_cross = disambiguate_causes(hash_cause, info_cross)
    assert res_cross["resolved_candidates"] == ["float_precision"]
    assert res_cross["runtime_candidates"] == ["hash_order_sensitivity"]
    assert res_cross["resolution"] == "refined"
    assert res_cross["refined"] is True
    assert res_cross["disambiguated"] is False
    assert res_cross["fault_side"] == "Test Side"

    for clean_name in (
        "test_suppressed_with_pytest_approx",
        "test_suppressed_with_math_isclose_and_round",
    ):
        info_clean = inspect_test_source(f"tests/test_float.py::{clean_name}", project_dir=tmp_path)
        assert info_clean["indicators"]["float_precision"] == [], clean_name

    diag = _sample_diagnosis()
    diag["tests"][1]["test_id"] = "tests/test_float.py::test_direct_float_literal_assert"
    diag["tests"][1]["suspected_causes"] = hash_cause
    md_zh = render_markdown(diag, project_dir=tmp_path, lang="zh")
    assert "浮点数精度断言隐患" in md_zh
    assert "对浮点数计算结果使用了严格相等断言" in md_zh
    assert "因对浮点数计算使用了严格相等断言，导致切换哈希种子或重复运行下发生精度翻转。" in md_zh
    assert "运行时怀疑根因 (hash_order_sensitivity -> 已由 AST 启发式细化):" in md_zh
    assert "ambiguous" not in md_zh
    assert "相同种子和相同顺序下结果仍发生波动" not in md_zh
    assert "asserts strict equality" not in md_zh

    md_en = render_markdown(diag, project_dir=tmp_path, lang="en")
    assert (
        "> - **Runtime suspected cause (hash_order_sensitivity -> refined by AST hint):** "
        "`float_precision` — Outcomes vary across hash seeds or repeated runs due to strict floating-point equality assertions."
    ) in md_en
    assert "ambiguous" not in md_en


def test_stage2_stage3_contract_refinement_and_external_io_reasons(tmp_path):
    """Task 3 regression: verify Stage 2 -> Stage 3 contract preservation, reason updates,
    and Section 2 / Section 3 rendering for resource_leak (refined with polluter suffix)
    and external_io (disambiguated)."""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir(exist_ok=True)
    (tests_dir / "test_contract.py").write_text(
        "import urllib.request\n\n"
        "def test_leak_with_polluter():\n"
        "    with open('shared_cache.txt', 'a') as f:\n"
        "        f.write('x')\n\n"
        "def test_net_io():\n"
        "    urllib.request.urlopen('https://example.com/status', timeout=1)\n",
        encoding="utf-8",
    )

    diag_leak = _sample_diagnosis()
    diag_leak["tests"][1]["test_id"] = "tests/test_contract.py::test_leak_with_polluter"
    diag_leak["tests"][1]["suspected_causes"] = [
        {
            "candidates": ["shared_state_interference"],
            "status": "suspected",
            "reason": (
                "Outcomes differ across test orders, or isolated executions pass while suite executions fail, "
                "with a consistent repeat baseline. Suspected polluting predecessor(s): test_setup_cache."
            ),
            "run_ids": [1, 2],
            "suspected_polluters": ["tests/test_contract.py::test_setup_cache"],
        }
    ]

    md_leak_en = render_markdown(diag_leak, project_dir=tmp_path, lang="en")
    assert (
        "> - **Runtime suspected cause (shared_state_interference -> refined by AST hint):** "
        "`resource_leak` — Outcomes vary across runs or test orders due to unisolated file or port resources. "
        "Suspected polluting predecessor(s): test_setup_cache."
    ) in md_leak_en
    assert "AST Refined *" in md_leak_en
    assert "ambiguous" not in md_leak_en

    md_leak_zh = render_markdown(diag_leak, project_dir=tmp_path, lang="zh")
    assert (
        "> - **运行时怀疑根因 (shared_state_interference -> 已由 AST 启发式细化):** "
        "`resource_leak` — 因未隔离的文件路径或固定网络端口资源冲突，导致跨轮次或不同执行顺序下结果发生波动。"
        " 疑似前置污染源测试：test_setup_cache。"
    ) in md_leak_zh
    assert "AST 细化 *" in md_leak_zh
    assert "ambiguous" not in md_leak_zh

    diag_io = _sample_diagnosis()
    diag_io["tests"][1]["test_id"] = "tests/test_contract.py::test_net_io"
    diag_io["tests"][1]["suspected_causes"] = [
        {
            "candidates": ["unseeded_randomness", "race_conditions"],
            "status": "ambiguous",
            "reason": (
                "Outcomes vary under the same recorded seed and collected order; "
                "source investigation is needed to distinguish these candidates."
            ),
            "run_ids": [1, 2],
        }
    ]
    md_io_en = render_markdown(diag_io, project_dir=tmp_path, lang="en")
    assert (
        "> - **Runtime Suspicion (`ambiguous` -> narrowed by AST heuristic hint):** "
        "`external_io` — Outcomes vary under repeated runs due to unmocked external network I/O."
    ) in md_io_en
    md_io_zh = render_markdown(diag_io, project_dir=tmp_path, lang="zh")
    assert (
        "> - **运行时怀疑根因 (`ambiguous` -> 已由 AST 启发式提示消歧):** "
        "`external_io` — 因存在未 Mock 拦截的外部网络请求，导致重复运行下结果发生波动或超时。"
    ) in md_io_zh






