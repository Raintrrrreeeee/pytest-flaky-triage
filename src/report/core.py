"""Validate diagnosis.json and render the Markdown triage report."""

from collections import Counter
import json
import math
from pathlib import Path
import re

from .heuristics import disambiguate_causes, inspect_test_source

CONDITIONS = ("repeat", "hashseed", "order", "isolated")
OUTCOMES = ("passed", "failed", "error", "skipped", "xfailed", "xpassed", "timeout", "not_run")
VALID_OUTCOMES = ("passed", "failed", "error")
EXCLUDED_OUTCOMES = ("skipped", "xfailed", "xpassed")
INVALID_OUTCOMES = ("timeout", "not_run")
VERDICTS = ("stable", "consistently_failing", "flaky", "inconclusive")
STATUSES = {"ok", "timeout", "interrupted", "internal_error", "usage_error", "no_tests", "crashed"}
CANDIDATES = {
    "unseeded_randomness",
    "race_conditions",
    "hash_order_sensitivity",
    "shared_state_interference",
}
VERDICT_ORDER = {
    "flaky": 0,
    "consistently_failing": 1,
    "inconclusive": 2,
    "stable": 3,
}
CATEGORY_ORDER = {
    ("hash_order_sensitivity",): 0,
    ("shared_state_interference",): 1,
    ("unseeded_randomness",): 2,
    ("race_conditions",): 3,
}
RATE_TOLERANCE = 1e-6


class ReportContractError(ValueError):
    """Raised when diagnosis.json violates the agreed schema_version 1 contract."""


def _is_int(value, minimum=None):
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return minimum is None or value >= minimum


def _is_nullable_number(value, low=0.0, high=None):
    if value is None:
        return True
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if not math.isfinite(float(value)) or float(value) < low:
        return False
    if high is not None and float(value) > high + 1e-9:
        return False
    return True


def _expected_verdict(counts, valid, min_valid=2):
    if valid < min_valid:
        return "inconclusive"
    passed = counts["passed"]
    nonpassing = counts["failed"] + counts["error"]
    if passed and nonpassing:
        return "flaky"
    if passed:
        return "stable"
    return "consistently_failing"


def _validate_summary_block(block, label, min_valid=2):
    if not isinstance(block, dict):
        raise ReportContractError(f"{label}: expected object")
    required = {
        "total", "valid", "counts", "excluded", "invalid",
        "stability_score", "failure_frequency", "error_frequency", "verdict",
    }
    if not required.issubset(block.keys()):
        raise ReportContractError(f"{label}: missing required aggregate summary fields")
    for field in ("total", "valid", "excluded", "invalid"):
        if not _is_int(block[field], 0):
            raise ReportContractError(f"{label}.{field}: expected nonnegative integer")
    counts = block["counts"]
    if not isinstance(counts, dict) or set(counts.keys()) != set(OUTCOMES):
        raise ReportContractError(f"{label}.counts: expected all eight outcome keys")
    for outcome, count in counts.items():
        if not _is_int(count, 0):
            raise ReportContractError(f"{label}.counts.{outcome}: expected nonnegative integer")

    expected_valid = sum(counts[o] for o in VALID_OUTCOMES)
    expected_excluded = sum(counts[o] for o in EXCLUDED_OUTCOMES)
    expected_invalid = sum(counts[o] for o in INVALID_OUTCOMES)
    expected_total = sum(counts.values())

    if block["valid"] != expected_valid:
        raise ReportContractError(
            f"{label}.valid: {block['valid']} disagrees with passed+failed+error ({expected_valid})"
        )
    if block["excluded"] != expected_excluded:
        raise ReportContractError(
            f"{label}.excluded: {block['excluded']} disagrees with skipped+xfailed+xpassed ({expected_excluded})"
        )
    if block["invalid"] != expected_invalid:
        raise ReportContractError(
            f"{label}.invalid: {block['invalid']} disagrees with timeout+not_run ({expected_invalid})"
        )
    if block["total"] != expected_total or block["total"] != (
        block["valid"] + block["excluded"] + block["invalid"]
    ):
        raise ReportContractError(
            f"{label}.total: {block['total']} disagrees with sum of outcomes ({expected_total})"
        )

    for rate in ("stability_score", "failure_frequency", "error_frequency"):
        if not _is_nullable_number(block[rate], 0.0, 1.0):
            raise ReportContractError(f"{label}.{rate}: expected number in [0, 1] or null")

    valid = block["valid"]
    if valid == 0:
        for rate in ("stability_score", "failure_frequency", "error_frequency"):
            if block[rate] is not None:
                raise ReportContractError(f"{label}.{rate}: must be null when valid == 0")
    else:
        expected_rates = {
            "stability_score": counts["passed"] / valid,
            "failure_frequency": counts["failed"] / valid,
            "error_frequency": counts["error"] / valid,
        }
        for rate, exp_val in expected_rates.items():
            actual_val = block[rate]
            if actual_val is None or not math.isclose(
                float(actual_val), exp_val, rel_tol=RATE_TOLERANCE, abs_tol=RATE_TOLERANCE
            ):
                raise ReportContractError(
                    f"{label}.{rate}: {actual_val!r} disagrees with {exp_val:.6f}"
                )

    if block["verdict"] not in VERDICTS:
        raise ReportContractError(f"{label}.verdict: unknown verdict {block['verdict']!r}")
    exp_verdict = _expected_verdict(counts, valid, min_valid=min_valid)
    if block["verdict"] != exp_verdict:
        raise ReportContractError(
            f"{label}.verdict: {block['verdict']!r} disagrees with counts-derived verdict {exp_verdict!r}"
        )


def validate_diagnosis(data):
    """Validate top-level and nested structures and invariants of schema_version 1."""
    if not isinstance(data, dict):
        raise ReportContractError("diagnosis root must be a JSON object")
    if not _is_int(data.get("schema_version")) or data["schema_version"] != 1:
        raise ReportContractError(f"unsupported schema_version: {data.get('schema_version')!r}")

    for top_key in ("policy", "summary", "runs", "tests"):
        if top_key not in data:
            raise ReportContractError(f"missing top-level key {top_key!r}")

    policy = data["policy"]
    if (
        not isinstance(policy, dict)
        or not isinstance(policy.get("stability_score"), str)
        or not isinstance(policy.get("scope"), str)
    ):
        raise ReportContractError("policy must be an object containing string stability_score and scope")
    min_valid = policy.get("minimum_valid_runs", 2)
    if not _is_int(min_valid, 1):
        raise ReportContractError("policy.minimum_valid_runs must be a positive integer")

    summary = data["summary"]
    if not isinstance(summary, dict):
        raise ReportContractError("summary must be an object")
    for k in ("tests", "runs", "records"):
        if not _is_int(summary.get(k), 0):
            raise ReportContractError(f"summary.{k} must be a nonnegative integer")
    verdicts = summary.get("verdicts")
    if not isinstance(verdicts, dict) or set(verdicts.keys()) != set(VERDICTS):
        raise ReportContractError("summary.verdicts must contain all four verdict keys")
    for v_name, v_count in verdicts.items():
        if not _is_int(v_count, 0):
            raise ReportContractError(f"summary.verdicts.{v_name} must be a nonnegative integer")

    runs = data["runs"]
    if not isinstance(runs, list):
        raise ReportContractError("runs must be a list")
    run_ids = set()
    for idx, run in enumerate(runs):
        if not isinstance(run, dict):
            raise ReportContractError(f"runs[{idx}] must be an object")
        for req in ("run_id", "condition", "condition_value", "hash_seed", "exit_code",
                    "status", "seconds", "tests_expected", "tests_reported", "log_path"):
            if req not in run:
                raise ReportContractError(f"runs[{idx}] missing {req!r}")
        if not _is_int(run["run_id"], 1):
            raise ReportContractError(f"runs[{idx}].run_id must be a positive integer")
        if run["run_id"] in run_ids:
            raise ReportContractError(f"runs[{idx}].run_id: duplicate run_id {run['run_id']}")
        run_ids.add(run["run_id"])

        if run["condition"] not in CONDITIONS:
            raise ReportContractError(f"runs[{idx}].condition invalid: {run['condition']!r}")
        if not isinstance(run["condition_value"], str) or not run["condition_value"].strip():
            raise ReportContractError(f"runs[{idx}].condition_value must be a non-empty string")
        if not _is_int(run["hash_seed"], 0):
            raise ReportContractError(f"runs[{idx}].hash_seed must be a nonnegative integer")
        if not _is_int(run["exit_code"]):
            raise ReportContractError(f"runs[{idx}].exit_code must be an integer")
        if not isinstance(run["status"], str) or run["status"] not in STATUSES:
            raise ReportContractError(f"runs[{idx}].status invalid: {run['status']!r}")
        if not _is_nullable_number(run["seconds"], 0.0):
            raise ReportContractError(f"runs[{idx}].seconds must be nonnegative number or null")
        if not _is_int(run["tests_expected"], 0):
            raise ReportContractError(f"runs[{idx}].tests_expected must be a nonnegative integer")
        if not _is_int(run["tests_reported"], 0):
            raise ReportContractError(f"runs[{idx}].tests_reported must be a nonnegative integer")
        if run["tests_reported"] > run["tests_expected"]:
            raise ReportContractError(
                f"runs[{idx}].tests_reported ({run['tests_reported']}) > tests_expected ({run['tests_expected']})"
            )
        if not isinstance(run["log_path"], str) or not run["log_path"].strip():
            raise ReportContractError(f"runs[{idx}].log_path must be a non-empty string")

    tests = data["tests"]
    if not isinstance(tests, list):
        raise ReportContractError("tests must be a list")
    test_ids = set()
    total_evidence_records = 0
    observed_verdicts = Counter()

    for idx, test in enumerate(tests):
        if not isinstance(test, dict):
            raise ReportContractError(f"tests[{idx}] must be an object")
        for req in ("test_id", "overall", "conditions", "suspected_causes", "warnings", "evidence"):
            if req not in test:
                raise ReportContractError(f"tests[{idx}] missing {req!r}")
        if not isinstance(test["test_id"], str) or not test["test_id"].strip():
            raise ReportContractError(f"tests[{idx}].test_id must be a non-empty string")
        if test["test_id"] in test_ids:
            raise ReportContractError(f"tests[{idx}].test_id: duplicate test_id {test['test_id']!r}")
        test_ids.add(test["test_id"])

        _validate_summary_block(test["overall"], f"tests[{idx}].overall", min_valid=min_valid)
        observed_verdicts[test["overall"]["verdict"]] += 1

        conds = test["conditions"]
        if not isinstance(conds, dict) or set(conds.keys()) != set(CONDITIONS):
            raise ReportContractError(f"tests[{idx}].conditions must contain all four conditions")
        for c_name in CONDITIONS:
            _validate_summary_block(
                conds[c_name], f"tests[{idx}].conditions.{c_name}", min_valid=min_valid
            )

        if not isinstance(test["warnings"], list) or not all(isinstance(w, str) for w in test["warnings"]):
            raise ReportContractError(f"tests[{idx}].warnings must be a list of strings")

        if not isinstance(test["evidence"], list):
            raise ReportContractError(f"tests[{idx}].evidence must be a list")
        seen_ev_runs = set()
        for e_idx, ev in enumerate(test["evidence"]):
            if not isinstance(ev, dict):
                raise ReportContractError(f"tests[{idx}].evidence[{e_idx}] must be an object")
            ev_rid = ev.get("run_id")
            if not _is_int(ev_rid, 1) or ev_rid not in run_ids:
                raise ReportContractError(
                    f"tests[{idx}].evidence[{e_idx}].run_id {ev_rid!r} does not reference a valid run"
                )
            if ev_rid in seen_ev_runs:
                raise ReportContractError(
                    f"tests[{idx}].evidence[{e_idx}].run_id: duplicate run_id {ev_rid}"
                )
            seen_ev_runs.add(ev_rid)
            pos = ev.get("position")
            if pos is not None and not _is_int(pos, 1):
                raise ReportContractError(f"tests[{idx}].evidence[{e_idx}].position invalid")
            if ev.get("outcome") not in OUTCOMES:
                raise ReportContractError(f"tests[{idx}].evidence[{e_idx}].outcome invalid")
        total_evidence_records += len(test["evidence"])

        if not isinstance(test["suspected_causes"], list):
            raise ReportContractError(f"tests[{idx}].suspected_causes must be a list")
        if test["overall"]["verdict"] != "flaky" and test["suspected_causes"]:
            raise ReportContractError(
                f"tests[{idx}].suspected_causes must be empty when verdict is {test['overall']['verdict']!r}"
            )
        for c_idx, cause in enumerate(test["suspected_causes"]):
            if not isinstance(cause, dict):
                raise ReportContractError(f"tests[{idx}].suspected_causes[{c_idx}] must be an object")
            cands = cause.get("candidates")
            if (
                not isinstance(cands, list)
                or not cands
                or not all(isinstance(c, str) and c in CANDIDATES for c in cands)
                or len(set(cands)) != len(cands)
            ):
                raise ReportContractError(f"tests[{idx}].suspected_causes[{c_idx}].candidates invalid")
            expected_status = "ambiguous" if len(cands) > 1 else "suspected"
            if cause.get("status") != expected_status:
                raise ReportContractError(
                    f"tests[{idx}].suspected_causes[{c_idx}].status {cause.get('status')!r} "
                    f"disagrees with candidates count ({expected_status!r})"
                )
            if not isinstance(cause.get("reason"), str) or not cause["reason"].strip():
                raise ReportContractError(f"tests[{idx}].suspected_causes[{c_idx}].reason must be non-empty string")
            r_ids = cause.get("run_ids")
            if (
                not isinstance(r_ids, list)
                or not r_ids
                or not all(_is_int(r, 1) and r in run_ids for r in r_ids)
                or len(set(r_ids)) != len(r_ids)
            ):
                raise ReportContractError(f"tests[{idx}].suspected_causes[{c_idx}].run_ids invalid")

    if summary["tests"] != len(tests):
        raise ReportContractError(f"summary.tests ({summary['tests']}) != len(tests) ({len(tests)})")
    if summary["runs"] != len(runs):
        raise ReportContractError(f"summary.runs ({summary['runs']}) != len(runs) ({len(runs)})")
    if summary["records"] != total_evidence_records:
        raise ReportContractError(
            f"summary.records ({summary['records']}) != total evidence records ({total_evidence_records})"
        )
    for v_name in VERDICTS:
        if summary["verdicts"][v_name] != observed_verdicts[v_name]:
            raise ReportContractError(
                f"summary.verdicts.{v_name} ({summary['verdicts'][v_name]}) != "
                f"observed count ({observed_verdicts[v_name]})"
            )

    return data


def load_diagnosis(path):
    """Read and validate a schema_version 1 diagnosis.json file."""
    with Path(path).open(encoding="utf-8") as stream:
        try:
            data = json.load(stream)
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise ReportContractError(f"invalid diagnosis JSON: {exc}") from exc
    return validate_diagnosis(data)


def _short_test_name(test_id):
    """Extract the function/parametrized name without splitting inside [...] brackets."""
    bracket_idx = test_id.find("[")
    prefix = test_id[:bracket_idx] if bracket_idx != -1 else test_id
    suffix = test_id[bracket_idx:] if bracket_idx != -1 else ""
    return prefix.split("::")[-1] + suffix


def _fmt_inline_code(text):
    """Format text safely as a Markdown inline code span inside a table cell."""
    cleaned = str(text).replace("\r", " ").replace("\n", " ").replace("|", "\\|")
    if "`" in cleaned:
        max_ticks = max((len(m.group(0)) for m in re.finditer(r"`+", cleaned)), default=1)
        fence = "`" * max(2, max_ticks + 1)
        return f"{fence} {cleaned} {fence}"
    return f"`{cleaned}`"


def _escape_table_cell(text):
    """Escape raw pipe and newline characters in a Markdown table cell."""
    return str(text).replace("\r", " ").replace("\n", " ").replace("|", "\\|")


def _fmt_score(summary_block):
    score = summary_block.get("stability_score")
    valid = summary_block.get("valid", 0)
    passed = summary_block.get("counts", {}).get("passed", 0)
    if score is None or valid == 0:
        return "N/A"
    return f"{score:.2f} ({passed}/{valid})"


def _fmt_cond_cell(summary_block):
    valid = summary_block.get("valid", 0)
    passed = summary_block.get("counts", {}).get("passed", 0)
    if valid == 0:
        return "N/A"
    return f"{passed}/{valid}"


def _fmt_fail_err(summary_block):
    fail = summary_block.get("failure_frequency")
    err = summary_block.get("error_frequency")
    valid = summary_block.get("valid", 0)
    if fail is None or err is None or valid == 0:
        return "N/A / N/A"
    return f"{fail:.2f} / {err:.2f}"


def _fmt_run_links(run_ids, runs_by_id, max_shown=4, evidence=None):
    """Format supporting run IDs and their validated log_path values, ensuring
    failing/error runs are always included when the run list is truncated."""
    if not run_ids:
        return "—"
    total = len(run_ids)
    if total <= max_shown:
        parts = [f"run {rid} (`{runs_by_id[rid]['log_path']}`)" for rid in run_ids]
        return ", ".join(parts)

    outcomes_by_run = {r["run_id"]: r.get("outcome") for r in (evidence or [])}
    failing_rids = [rid for rid in run_ids if outcomes_by_run.get(rid) in ("failed", "error")]
    passing_rids = [rid for rid in run_ids if outcomes_by_run.get(rid) == "passed"]

    if failing_rids and passing_rids:
        selected = []
        # Include first passing run (baseline) and first failing run(s) plus trailing run
        selected.append(passing_rids[0])
        for rid in failing_rids:
            if rid not in selected and len(selected) < max_shown - 1:
                selected.append(rid)
        if run_ids[-1] not in selected and len(selected) < max_shown:
            selected.append(run_ids[-1])
        for rid in run_ids:
            if rid not in selected and len(selected) < max_shown:
                selected.append(rid)
        selected.sort(key=lambda r: run_ids.index(r))
        head_parts = [f"run {rid} (`{runs_by_id[rid]['log_path']}`)" for rid in selected[:2]]
        tail_parts = [f"run {rid} (`{runs_by_id[rid]['log_path']}`)" for rid in selected[2:]]
        return f"{', '.join(head_parts)}, ..., {', '.join(tail_parts)} ({total} runs)"

    # Fallback: show both leading and trailing runs so isolated/order runs at the end are visible
    head_count = max_shown // 2
    tail_count = max_shown - head_count
    head_parts = [f"run {rid} (`{runs_by_id[rid]['log_path']}`)" for rid in run_ids[:head_count]]
    tail_parts = [f"run {rid} (`{runs_by_id[rid]['log_path']}`)" for rid in run_ids[-tail_count:]]
    return f"{', '.join(head_parts)}, ..., {', '.join(tail_parts)} ({total} runs)"


def _format_markdown_table(headers, alignments, rows):
    """Format a Markdown table with padded column widths for clean visual alignment."""
    widths = [
        max(len(headers[i]), *(len(r[i]) for r in rows), 3) if rows else max(len(headers[i]), 3)
        for i in range(len(headers))
    ]

    def pad_cell(text, width, align):
        if align == "center":
            return text.center(width)
        if align == "right":
            return text.rjust(width)
        return text.ljust(width)

    header_line = "| " + " | ".join(headers[i].ljust(widths[i]) for i in range(len(headers))) + " |"
    sep_cells = []
    for i, align in enumerate(alignments):
        w = widths[i]
        if align == "center":
            sep_cells.append(":" + "-" * (w - 2) + ":")
        elif align == "right":
            sep_cells.append("-" * (w - 1) + ":")
        else:
            sep_cells.append(":" + "-" * (w - 1))
    sep_line = "| " + " | ".join(sep_cells) + " |"

    body_lines = [
        "| " + " | ".join(pad_cell(row[i], widths[i], alignments[i]) for i in range(len(headers))) + " |"
        for row in rows
    ]
    return [header_line, sep_line, *body_lines]


def _item_sort_key(item):
    test = item["test"]
    res = item["resolution"]
    verdict = test["overall"]["verdict"]
    v_rank = VERDICT_ORDER.get(verdict, 9)
    cands = tuple(res["resolved_candidates"])
    if not cands:
        c_rank = 5
    else:
        c_rank = CATEGORY_ORDER.get(cands, 4)
    score = test["overall"]["stability_score"]
    s_rank = score if score is not None else -1.0
    return (v_rank, c_rank, s_rank, test["test_id"])


def generate_report(diagnosis, project_dir=None):
    """Enrich validated diagnosis data with source-based heuristic hints."""
    validate_diagnosis(diagnosis)
    runs_by_id = {r["run_id"]: r for r in diagnosis["runs"]}
    enriched_tests = []
    for test in diagnosis["tests"]:
        source_info = inspect_test_source(test["test_id"], project_dir=project_dir)
        resolution = disambiguate_causes(test["suspected_causes"], source_info)
        test["resolution"] = resolution
        enriched_tests.append({
            "test": test,
            "source_info": source_info,
            "resolution": resolution,
        })
    return {
        "diagnosis": diagnosis,
        "runs_by_id": runs_by_id,
        "enriched_tests": enriched_tests,
    }


def render_markdown(diagnosis, project_dir=None):
    """Render a concise, visually aligned Markdown triage report."""
    bundle = generate_report(diagnosis, project_dir=project_dir)
    diag = bundle["diagnosis"]
    runs_by_id = bundle["runs_by_id"]
    enriched = sorted(bundle["enriched_tests"], key=_item_sort_key)
    summary = diag["summary"]
    verdicts = summary["verdicts"]
    policy = diag["policy"]

    abnormal_runs = [
        r for r in diag["runs"]
        if r["status"] != "ok" or r["tests_reported"] < r["tests_expected"]
    ]

    lines = [
        "# Test-Stability Triage Report",
        "",
        "## 1. Overview Summary",
        "",
        f"- **Tests Analysed:** {summary['tests']}",
        f"- **Total Matrix Runs:** {summary['runs']} ({summary['records']} recorded observations)",
        f"- **Verdict Breakdown:** "
        f"`flaky`: {verdicts['flaky']} | "
        f"`stable`: {verdicts['stable']} | "
        f"`consistently_failing`: {verdicts['consistently_failing']} | "
        f"`inconclusive`: {verdicts['inconclusive']}",
        f"- **Abnormal / Incomplete Runs:** {len(abnormal_runs)}",
        f"- **Scoring Policy:** `{policy['stability_score']}` (minimum valid runs: {policy.get('minimum_valid_runs', 2)})",
        f"- **Interpretation Note:** {policy['scope']} Stability scores measure observed pass frequency, not confidence in a proven root cause. Source-based indicators below are heuristic hints to help disambiguate runtime candidates.",
        "",
        "## 2. Test Triage Summary Table",
        "",
    ]

    s2_headers = [
        "Test", "Verdict", "Score", "Repeat", "Hashseed",
        "Order", "Isolated", "Suspected Cause", "Fault Side", "Basis",
    ]
    s2_alignments = [
        "left", "center", "center", "center", "center",
        "center", "center", "left", "center", "left",
    ]
    s2_rows = []
    any_disambiguated = False
    short_counts = Counter(_short_test_name(it["test"]["test_id"]) for it in enriched)

    for item in enriched:
        test = item["test"]
        res = item["resolution"]
        if res["disambiguated"]:
            any_disambiguated = True
        raw_short = _short_test_name(test["test_id"])
        short_name = test["test_id"] if short_counts[raw_short] > 1 else raw_short
        verdict = f"`{test['overall']['verdict']}`"
        overall_score = _fmt_score(test["overall"])
        conds = test["conditions"]
        rep_cell = _fmt_cond_cell(conds["repeat"])
        hash_cell = _fmt_cond_cell(conds["hashseed"])
        ord_cell = _fmt_cond_cell(conds["order"])
        iso_cell = _fmt_cond_cell(conds["isolated"])

        resolved_cands = res["resolved_candidates"]
        cause_str = " / ".join(f"`{c}`" for c in resolved_cands) if resolved_cands else "—"
        side_str = (
            res["fault_side"]
            if resolved_cands and res["fault_side"] not in ("N/A", "Undetermined")
            else "—"
        )
        basis_str = res["basis"] if resolved_cands else "—"

        s2_rows.append([
            _fmt_inline_code(short_name),
            verdict,
            overall_score,
            rep_cell,
            hash_cell,
            ord_cell,
            iso_cell,
            cause_str,
            side_str,
            basis_str,
        ])

    lines.extend(_format_markdown_table(s2_headers, s2_alignments, s2_rows))
    if any_disambiguated:
        lines.extend([
            "",
            "*\\* `AST Hint *`: Runtime evidence was `ambiguous` between `unseeded_randomness` and `race_conditions` (outcomes varied under identical seed and test order); narrowed via static AST heuristic inspection.*",
        ])

    lines.extend([
        "",
        "## 3. Diagnostic Details and Evidence Traceability",
        "",
    ])

    non_stable = [
        item for item in enriched
        if item["test"]["overall"]["verdict"] != "stable" or item["test"]["warnings"]
    ]
    if not non_stable:
        lines.append("All analysed tests were observed as `stable` with no warnings.")
    else:
        groups = []
        groups_by_key = {}
        for item in non_stable:
            key = tuple(item["resolution"]["resolved_candidates"])
            if key not in groups_by_key:
                groups_by_key[key] = []
                groups.append((key, groups_by_key[key]))
            groups_by_key[key].append(item)

        for idx, (cands_key, group_items) in enumerate(groups, start=1):
            if idx > 1:
                lines.extend(["---", ""])

            if len(cands_key) == 1:
                title_label = f"`{cands_key[0]}`"
            elif cands_key:
                title_label = " / ".join(f"`{c}`" for c in cands_key) + " (ambiguous)"
            else:
                title_label = "Unclassified / Non-Flaky & Warnings"

            group_sides = []
            for it in group_items:
                fs = it["resolution"]["fault_side"]
                if fs not in ("N/A", "Undetermined", "—") and fs not in group_sides:
                    group_sides.append(fs)
            side_heading = f" — {' / '.join(group_sides)}" if group_sides else ""
            count_label = f"{len(group_items)} test{'s' if len(group_items) != 1 else ''}"
            lines.append(f"### 3.{idx} {title_label}{side_heading} ({count_label})")
            lines.append("")

            # Collect all distinct runtime (status, candidates, reason) tuples across tests in this group
            distinct_reasons = []
            per_test_reason_needed = False
            reason_signatures_by_test = []
            for it in group_items:
                t_sigs = []
                for cause in it["test"]["suspected_causes"]:
                    sig = (
                        cause["status"],
                        tuple(cause["candidates"]),
                        cause["reason"],
                        it["resolution"]["disambiguated"],
                    )
                    t_sigs.append(sig)
                    if sig not in distinct_reasons:
                        distinct_reasons.append(sig)
                reason_signatures_by_test.append(t_sigs)
                if len(t_sigs) > 1:
                    per_test_reason_needed = True

            if len(distinct_reasons) > 1:
                per_test_reason_needed = True

            if distinct_reasons and not per_test_reason_needed:
                status, raw_cands, reason, is_disamb = distinct_reasons[0]
                raw_cands_fmt = " / ".join(f"`{c}`" for c in raw_cands)
                status_label = "`ambiguous` -> narrowed by AST heuristic hint" if is_disamb else f"`{status}`"
                lines.append(
                    f"> - **Runtime Suspicion ({status_label}):** {raw_cands_fmt} — {reason}"
                )
            elif distinct_reasons:
                for it in group_items:
                    t = it["test"]
                    for cause in t["suspected_causes"]:
                        raw_cands_fmt = " / ".join(f"`{c}`" for c in cause["candidates"])
                        status_label = (
                            "`ambiguous` -> narrowed by AST heuristic hint"
                            if it["resolution"]["disambiguated"]
                            else f"`{cause['status']}`"
                        )
                        lines.append(
                            f"> - **Runtime Suspicion ({_fmt_inline_code(t['test_id'])}, {status_label}):** "
                            f"{raw_cands_fmt} — {cause['reason']}"
                        )
            else:
                lines.append("> - **Runtime Suspicion:** None assigned from runtime condition evidence.")
            lines.append("")

            s3_headers = [
                "Test", "Stability", "Fail / Err", "Supporting Runs (Logs)", "Source Heuristic Hints",
            ]
            s3_alignments = ["left", "center", "center", "left", "left"]
            s3_rows = []
            group_warnings = []

            for it in group_items:
                t = it["test"]
                r = it["resolution"]
                s_info = it["source_info"]
                ov = t["overall"]
                raw_short = _short_test_name(t["test_id"])
                short_name = t["test_id"] if short_counts[raw_short] > 1 else raw_short

                # Collect supporting runs per test (preserving each test's exact evidence run_ids)
                if not t["suspected_causes"]:
                    runs_cell = "—"
                elif len(t["suspected_causes"]) == 1:
                    runs_cell = _fmt_run_links(
                        t["suspected_causes"][0]["run_ids"], runs_by_id, evidence=t.get("evidence")
                    )
                else:
                    parts = []
                    for c_obj in t["suspected_causes"]:
                        c_tag = "/".join(f"`{c}`" for c in c_obj["candidates"])
                        parts.append(
                            f"{c_tag}: {_fmt_run_links(c_obj['run_ids'], runs_by_id, max_shown=4, evidence=t.get('evidence'))}"
                        )
                    runs_cell = "; ".join(parts)

                combined_hints = list(r.get("compact_hints", [])) + list(r.get("conflicting_hints", []))
                if combined_hints:
                    hint_cell = _escape_table_cell("; ".join(combined_hints))
                elif t["suspected_causes"]:
                    if not s_info.get("available"):
                        hint_cell = "Source not available for AST check"
                    else:
                        hint_cell = "No matching AST pattern"
                elif ov["verdict"] == "flaky":
                    # Flaky test with no runtime cause assigned: surface any AST hints if source is available
                    unmatched = []
                    for cat_name, hits in (s_info.get("indicators") or {}).items():
                        for hit in hits:
                            unmatched.append(
                                f"AST hint (no runtime cause): `{hit['location']}` {hit['detail']} "
                                f"(`{cat_name}`?, {hit['side']})"
                            )
                    if unmatched:
                        hint_cell = _escape_table_cell("; ".join(unmatched))
                    elif not s_info.get("available"):
                        hint_cell = "Source not available for AST check"
                    else:
                        hint_cell = "No matching AST pattern"
                else:
                    hint_cell = "—"

                s3_rows.append([
                    _fmt_inline_code(short_name),
                    _fmt_score(ov),
                    _fmt_fail_err(ov),
                    _escape_table_cell(runs_cell),
                    hint_cell,
                ])
                for w in t["warnings"]:
                    group_warnings.append((t["test_id"], w))

            lines.extend(_format_markdown_table(s3_headers, s3_alignments, s3_rows))
            if group_warnings:
                lines.append("")
                for t_id, w_text in group_warnings:
                    lines.append(f"- **Warning ({_fmt_inline_code(t_id)}):** {w_text}")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"
