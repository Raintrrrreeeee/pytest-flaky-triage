"""Observed pass rates, verdicts and conservative condition-based suspicions."""

from collections import Counter, defaultdict

from .records import CONDITIONS, OUTCOMES

VALID = {"passed", "failed", "error"}
VERDICTS = ("stable", "consistently_failing", "flaky", "inconclusive")


def _summary(rows):
    counts = Counter(r["outcome"] for r in rows)
    valid = sum(counts[o] for o in VALID)
    passed = counts["passed"]
    nonpassing = counts["failed"] + counts["error"]
    verdict = "inconclusive"
    if valid >= 2:
        verdict = "flaky" if passed and nonpassing else "stable" if passed else "consistently_failing"
    return {
        "total": len(rows), "valid": valid,
        "counts": {o: counts[o] for o in OUTCOMES},
        "excluded": sum(counts[o] for o in ("skipped", "xfailed", "xpassed")),
        "invalid": counts["timeout"] + counts["not_run"],
        "stability_score": passed / valid if valid else None,
        "failure_frequency": counts["failed"] / valid if valid else None,
        "error_frequency": counts["error"] / valid if valid else None,
        "verdict": verdict,
    }


def _mixed(rows):
    outcomes = {r["outcome"] == "passed" for r in rows if r["outcome"] in VALID}
    return len(outcomes) == 2


def _between_values(rows, baseline=None):
    """Opposing outcomes across condition values or relative to a consistent baseline,
    without observed within-value flips."""
    grouped = defaultdict(list)
    for row in rows:
        if row["outcome"] in VALID:
            grouped[row["condition_value"]].append(row)
    if not grouped:
        return False
    if any(_mixed(group) for group in grouped.values()):
        return False
    if len(grouped) >= 2 and _mixed(rows):
        return True
    if baseline and not _mixed(baseline) and _mixed(list(baseline) + list(rows)):
        return True
    return False


def _find_suspected_polluters(test_id, usable_rows, precedents_by_run):
    """Identify tests that consistently preceded `test_id` when it failed in
    full-suite runs, but did not precede it when it passed."""
    suite_rows = [
        r for r in usable_rows
        if r["condition"] in ("repeat", "hashseed", "order", "tz")
        and r.get("position") is not None
    ]
    failed_runs = [r for r in suite_rows if r["outcome"] != "passed"]
    passed_runs = [r for r in suite_rows if r["outcome"] == "passed"]
    if not failed_runs or not passed_runs:
        return []

    fail_pred_sets = []
    for r in failed_runs:
        preds = precedents_by_run.get((r["run_id"], test_id))
        if preds is not None:
            fail_pred_sets.append(preds)
    if not fail_pred_sets:
        return []

    common_on_fail = set.intersection(*fail_pred_sets)
    if not common_on_fail:
        return []

    ever_on_pass = set()
    always_on_pass = None
    for r in passed_runs:
        preds = precedents_by_run.get((r["run_id"], test_id), set())
        ever_on_pass |= preds
        always_on_pass = set(preds) if always_on_pass is None else (always_on_pass & preds)

    strict_candidates = common_on_fail - ever_on_pass
    if strict_candidates:
        return sorted(strict_candidates)
    candidates = common_on_fail - (always_on_pass or set())
    return sorted(candidates)


def _causes(rows, runs, verdict, test_id=None, precedents_by_run=None):
    if verdict != "flaky":
        return []
    # Completed individual outcomes remain scoreable after a crash, but abnormal
    # or incomplete runs must not establish a condition-based suspicion.
    usable = [
        r for r in rows
        if runs[r["run_id"]]["status"] == "ok"
        and runs[r["run_id"]]["tests_reported"] == runs[r["run_id"]]["tests_expected"]
        and r["outcome"] in VALID
    ]
    groups = {c: [r for r in usable if r["condition"] == c] for c in CONDITIONS}
    repeat = groups["repeat"]
    causes = []

    def add(candidates, reason, evidence, polluters=None):
        entry = {
            "candidates": candidates,
            "status": "ambiguous" if len(candidates) > 1 else "suspected",
            "reason": reason,
            "run_ids": sorted({r["run_id"] for r in evidence}),
        }
        if polluters:
            entry["suspected_polluters"] = polluters
        causes.append(entry)

    if _mixed(repeat):
        add(
            ["unseeded_randomness", "race_conditions"],
            "Outcomes vary under the same recorded seed and collected order; source investigation is needed to distinguish these candidates.",
            repeat,
        )
        return causes

    # A repeat baseline guards against labelling ordinary random variation as a
    # hash/order effect. Two repeats are only an evidence floor, not proof.
    if len(repeat) < 2:
        return causes

    hash_rows = groups["hashseed"]
    order_rows = groups["order"]
    tz_rows = groups["tz"]
    isolated = groups["isolated"]
    suite = [r for r in usable if r["condition"] in ("repeat", "order")]

    hash_effect = _between_values(hash_rows, baseline=repeat)
    order_effect = _between_values(order_rows, baseline=repeat)
    tz_effect = _between_values(tz_rows, baseline=repeat)

    # Bidirectional isolation effect:
    # 1. Polluted victim: fails in suite, passes when isolated
    # 2. State consumer (missing setup): passes in suite, fails when isolated
    victim_isolation = (
        bool(isolated)
        and all(r["outcome"] == "passed" for r in isolated)
        and any(r["outcome"] != "passed" for r in suite)
    )
    consumer_isolation = (
        bool(isolated)
        and all(r["outcome"] != "passed" for r in isolated)
        and all(r["outcome"] == "passed" for r in suite)
    )
    isolation_effect = victim_isolation or consumer_isolation

    # Guard against small-sample repeat non-flips on probabilistic tests:
    # 1. If a test flips across two or more independent single-factor perturbations
    #    (hashseed, order, tz) in a multi-run matrix (len(repeat) >= 5), a single-factor
    #    bug cannot explain both; or
    # 2. If a test has only a single rare failure across a full matrix (>= 10 usable
    #    runs) without an isolation recovery effect or deterministic timezone flip.
    total_nonpassing = sum(r["outcome"] != "passed" for r in usable)
    total_passing = sum(r["outcome"] == "passed" for r in usable)
    rare_single_flip = (
        len(usable) >= 10
        and min(total_nonpassing, total_passing) == 1
        and not isolation_effect
        and not tz_effect
    )
    single_perturbation_flips = sum([bool(hash_effect), bool(order_effect), bool(tz_effect)])
    multi_perturbation_flip = (
        len(repeat) >= 5
        and single_perturbation_flips >= 2
        and not victim_isolation
    )
    if rare_single_flip or multi_perturbation_flip:
        add(
            ["unseeded_randomness", "race_conditions"],
            "Outcomes were uniform during the finite repeat window but flipped across independent perturbations; source investigation is needed to distinguish probabilistic randomness or concurrency races.",
            usable,
        )
        return causes

    if hash_effect:
        add(
            ["hash_order_sensitivity"],
            "Outcomes differ across hash seeds while repeat outcomes are consistent.",
            repeat + hash_rows,
        )
    if order_effect or isolation_effect:
        polluters = (
            _find_suspected_polluters(test_id, usable, precedents_by_run)
            if test_id and precedents_by_run
            else []
        )
        reason = (
            "Outcomes differ across test orders, or isolated executions differ from suite executions, with a consistent repeat baseline."
            if consumer_isolation and not (order_effect or victim_isolation)
            else "Outcomes differ across test orders, or isolated executions pass while suite executions fail, with a consistent repeat baseline."
        )
        if polluters:
            short_p = ", ".join(p.split("::")[-1] for p in polluters[:3])
            reason = f"{reason} Suspected polluting predecessor(s): {short_p}."
        add(
            ["shared_state_interference"],
            reason,
            repeat + order_rows + isolated,
            polluters=polluters,
        )
    if tz_effect:
        add(
            ["timezone_sensitivity"],
            "Outcomes differ across timezones (TZ) while repeat outcomes are consistent.",
            repeat + tz_rows,
        )
    return causes


def _build_precedents_by_run(rows, runs):
    """Map (run_id, test_id) -> set of test_ids that executed earlier in the same run."""
    by_run = defaultdict(list)
    for row in rows:
        if row.get("position") is not None:
            by_run[row["run_id"]].append((row["position"], row["test_id"]))
    precedents = {}
    for run_id, pos_items in by_run.items():
        if runs[run_id]["condition"] == "isolated":
            continue
        pos_items.sort(key=lambda item: item[0])
        seen_before = set()
        for _, tid in pos_items:
            precedents[(run_id, tid)] = set(seen_before)
            seen_before.add(tid)
    return precedents


def analyse(rows, runs):
    """Analyse records from load_records; no source-code scan or final report."""
    by_test = defaultdict(list)
    for row in rows:
        by_test[row["test_id"]].append(row)
    precedents_by_run = _build_precedents_by_run(rows, runs)
    tests = []
    for test_id, observations in sorted(by_test.items()):
        observations = sorted(observations, key=lambda r: r["run_id"])
        overall = _summary(observations)
        conditions = {c: _summary([r for r in observations if r["condition"] == c]) for c in CONDITIONS}
        warnings = []
        if overall["valid"] < 2:
            warnings.append("Fewer than two valid executions; verdict is inconclusive.")
        if overall["excluded"] or overall["invalid"]:
            warnings.append("Excluded/invalid observations do not contribute to the score or verdict.")
        if any(runs[r["run_id"]]["status"] != "ok" for r in observations):
            warnings.append("Abnormal runs exist; completed outcomes contribute to scores but not suspected causes.")
        incomplete_runs = sorted({
            r["run_id"] for r in observations
            if runs[r["run_id"]]["tests_reported"] < runs[r["run_id"]]["tests_expected"]
        })
        if incomplete_runs:
            run_ids = ", ".join(str(run_id) for run_id in incomplete_runs)
            warnings.append(
                f"Incomplete runs excluded from suspected-cause inference: {run_ids}. "
                "Their completed outcomes still contribute to scores and verdicts."
            )
        missing = [c for c in CONDITIONS if not conditions[c]["valid"]]
        if missing:
            warnings.append("No valid evidence for conditions: " + ", ".join(missing))
        causes = _causes(
            observations,
            runs,
            overall["verdict"],
            test_id=test_id,
            precedents_by_run=precedents_by_run,
        )
        if overall["verdict"] == "flaky" and not causes:
            warnings.append("Mixed outcomes observed, but insufficient unconfounded evidence to assign a suspected cause.")
        tests.append({
            "test_id": test_id, "overall": overall, "conditions": conditions,
            "suspected_causes": causes, "warnings": warnings,
            "evidence": observations,
        })
    totals = Counter(test["overall"]["verdict"] for test in tests)
    # Refine multi-candidate suspected_polluters when a symmetric mutual polluter exists
    polluter_map = {}
    for test in tests:
        for cause in test["suspected_causes"]:
            if "suspected_polluters" in cause:
                polluter_map[test["test_id"]] = set(cause["suspected_polluters"])
    for test in tests:
        tid = test["test_id"]
        for cause in test["suspected_causes"]:
            plist = cause.get("suspected_polluters")
            if plist and len(plist) > 1:
                mutual = sorted(p for p in plist if tid in polluter_map.get(p, set()))
                if mutual:
                    cause["suspected_polluters"] = mutual
                    short_p = ", ".join(p.split("::")[-1] for p in mutual[:3])
                    base_reason = cause["reason"].split(" Suspected polluting predecessor(s):")[0]
                    cause["reason"] = f"{base_reason} Suspected polluting predecessor(s): {short_p}."

    return {
        "schema_version": 1,
        "policy": {
            "stability_score": "passed / (passed + failed + error)",
            "valid_outcomes": sorted(VALID), "minimum_valid_runs": 2,
            "scope": "Observed executions only; suspected causes are correlations, not proven root causes.",
        },
        "summary": {"tests": len(tests), "runs": len(runs), "records": len(rows),
                    "verdicts": {v: totals[v] for v in VERDICTS}},
        "runs": [runs[key] for key in sorted(runs)],
        "tests": tests,
    }
