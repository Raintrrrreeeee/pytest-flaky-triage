"""Validate diagnosis.json and render the Markdown triage report."""

from collections import Counter
import json
import math
from pathlib import Path
import re
import unicodedata

from .heuristics import disambiguate_causes, inspect_test_source

CONDITIONS = ("repeat", "hashseed", "order", "tz", "isolated")
REQUIRED_CONDITIONS = ("repeat", "hashseed", "order", "isolated")
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
    "timezone_sensitivity",
    "resource_leak",
    "external_io",
    "float_precision",
}
VERDICT_ORDER = {
    "flaky": 0,
    "consistently_failing": 1,
    "inconclusive": 2,
    "stable": 3,
}
CATEGORY_ORDER = {
    ("hash_order_sensitivity",): 0,
    ("float_precision",): 1,
    ("shared_state_interference",): 2,
    ("resource_leak",): 3,
    ("timezone_sensitivity",): 4,
    ("unseeded_randomness",): 5,
    ("race_conditions",): 6,
    ("external_io",): 7,
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
        if (
            not isinstance(conds, dict)
            or not set(REQUIRED_CONDITIONS).issubset(conds.keys())
            or not set(conds.keys()).issubset(CONDITIONS)
        ):
            raise ReportContractError(f"tests[{idx}].conditions must contain all required conditions")
        for c_name in conds:
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


def _display_width(text):
    """Return monospace terminal column width (East Asian Wide/Fullwidth = 2 cols)."""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _format_markdown_table(headers, alignments, rows):
    """Format a Markdown table with padded column widths for clean visual alignment."""
    widths = [
        max(_display_width(headers[i]), *(_display_width(r[i]) for r in rows), 3)
        if rows
        else max(_display_width(headers[i]), 3)
        for i in range(len(headers))
    ]

    def pad_cell(text, width, align):
        adj_width = max(len(text), width - (_display_width(text) - len(text)))
        if align == "center":
            return text.center(adj_width)
        if align == "right":
            return text.rjust(adj_width)
        return text.ljust(adj_width)

    header_line = (
        "| " + " | ".join(pad_cell(headers[i], widths[i], "left") for i in range(len(headers))) + " |"
    )
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
        c_rank = 9
    else:
        c_rank = CATEGORY_ORDER.get(cands, 8)
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


_ZH_CAUSE_LABELS = {
    "hash_order_sensitivity": "哈希顺序敏感",
    "shared_state_interference": "共享状态污染",
    "timezone_sensitivity": "时区/本地时间敏感",
    "unseeded_randomness": "未固定随机种子",
    "race_conditions": "并发时间竞态",
    "resource_leak": "文件与端口资源冲突",
    "external_io": "未隔离的外部网络依赖",
    "float_precision": "浮点数精度断言隐患",
}

_ZH_SIDE_LABELS = {
    "Test Side": "测试侧 (Test Side)",
    "Code Side": "业务侧 (Code Side)",
    "Both": "双侧 (Both)",
    "Test Side / Code Side": "双侧 (Test / Code Side)",
    "Code Side / Test Side": "双侧 (Code / Test Side)",
}

_ZH_BASIS_LABELS = {
    "Runtime+AST": "运行时+AST",
    "AST Hint *": "AST 消歧 *",
    "AST Refined *": "AST 细化 *",
    "Runtime Only": "仅运行时",
    "Runtime": "仅运行时",
    "Ambiguous": "存在歧义",
    "Runtime (Conflict)": "运行时 (AST 冲突)",
    "Runtime (AST conflict)": "运行时 (AST 冲突)",
}


def _translate_reason_zh(reason):
    text = reason
    polluter_suffix = ""
    if " Suspected polluting predecessor(s): " in text:
        text, p_part = text.split(" Suspected polluting predecessor(s): ", 1)
        polluter_suffix = f" 疑似前置污染源测试：{p_part.rstrip('.')}。"
    mapping = {
        "Outcomes vary under the same recorded seed and collected order; source investigation is needed to distinguish these candidates.": (
            "在相同哈希种子与相同执行顺序下结果仍发生波动；需结合源码 AST 检查区分随机性与并发竞态。"
        ),
        "Outcomes were uniform during the finite repeat window but flipped across independent perturbations; source investigation is needed to distinguish probabilistic randomness or concurrency races.": (
            "在有限次重复基线中结果一致，但在独立扰动维度间发生翻转；需结合源码 AST 检查区分低概率随机性与并发竞态。"
        ),
        "Outcomes differ across hash seeds while repeat outcomes are consistent.": (
            "固定条件下重复运行结果一致，但切换 PYTHONHASHSEED 哈希种子时结果发生翻转。"
        ),
        "Outcomes differ across test orders, or isolated executions pass while suite executions fail, with a consistent repeat baseline.": (
            "重复运行基线一致，但改变测试执行顺序时结果翻转（或在套件中失败、单测隔离时通过）。"
        ),
        "Outcomes differ across test orders, or isolated executions differ from suite executions, with a consistent repeat baseline.": (
            "重复运行基线一致，但改变测试执行顺序或单测隔离运行时结果与套件运行不一致。"
        ),
        "Outcomes differ across timezones (TZ) while repeat outcomes are consistent.": (
            "固定条件下重复运行结果一致，但切换 TZ 时区环境变量时结果发生翻转。"
        ),
        "Outcomes vary across runs or test orders due to unisolated file or port resources.": (
            "因未隔离的文件路径或固定网络端口资源冲突，导致跨轮次或不同执行顺序下结果发生波动。"
        ),
        "Outcomes vary under repeated runs due to unmocked external network I/O.": (
            "因存在未 Mock 拦截的外部网络请求，导致重复运行下结果发生波动或超时。"
        ),
        "Outcomes vary across hash seeds or repeated runs due to strict floating-point equality assertions.": (
            "因对浮点数计算使用了严格相等断言，导致切换哈希种子或重复运行下发生精度翻转。"
        ),
    }
    return mapping.get(text, text) + polluter_suffix


def _translate_hint_zh(hint):
    text = hint
    text = text.replace("Conflicting AST hint:", "冲突的 AST 提示：")
    text = text.replace("Conflicting hint:", "冲突的 AST 提示：")
    text = text.replace("AST hint (no runtime cause):", "AST 提示（无运行时归因）：")
    text = text.replace(", runtime suggests ", "，但运行时证据指向 ")
    text = text.replace("?, Test Side)", "?，测试侧)")
    text = text.replace("?, Code Side)", "?，业务侧)")
    text = text.replace("unsorted `join(...)` on unordered `set` from", "对来自以下位置的无序 `set` 执行了未排序的 `join(...)`：")
    text = text.replace(
        "unsorted string `join(...)` (verify argument ordering is deterministic)",
        "对字符串执行了未排序的 `join(...)`（请确认参数顺序是否确定）",
    )
    text = text.replace("iterates or converts unordered `set` without sorting", "对无序 `set` 进行了未排序的遍历或转换")
    text = text.replace("accesses/mutates shared module-level object", "读写/修改了模块级共享对象")
    text = text.replace("mutates shared module-level variable", "通过 `global` 修改了模块级共享变量")
    text = text.replace("accesses/mutates shared module-level state", "读写/修改了业务模块级共享状态")
    text = text.replace("accesses/mutates shared class-level attribute", "读写/修改了共享类属性")
    text = text.replace("(declared at ", "（声明于 ")
    text = text.replace(
        "operates on hardcoded file/database path via ",
        "通过以下调用操作了硬编码的文件/数据库路径：",
    )
    text = text.replace(
        " without `tmp_path` or `tempfile`",
        "（未使用 `tmp_path` 或 `tempfile` 隔离）",
    )
    text = text.replace(
        "binds hardcoded non-zero network port via ",
        "通过以下调用绑定了硬编码的非零网络端口：",
    )
    text = text.replace(
        "performs unmocked external network I/O via ",
        "通过以下调用发起了未 Mock 拦截的外部网络请求：",
    )
    text = text.replace(
        "asserts strict equality (`==`/`!=`) on floating-point values without `pytest.approx` or `math.isclose`",
        "对浮点数计算结果使用了严格相等断言（`==`/`!=`），未使用 `pytest.approx` 或 `math.isclose`",
    )
    text = text.replace(
        "asserts strict equality (`==`/`!=`) on floating-point return from ",
        "对来自以下位置的浮点数返回值使用了严格相等断言（`==`/`!=`）：",
    )
    text = text.replace(
        " without `pytest.approx` or `math.isclose`",
        "（未使用 `pytest.approx` 或 `math.isclose`）",
    )
    text = text.replace(" without a fixed seed", " 且未固定随机种子")
    text = text.replace(
        " without explicit timezone (`tz`) or time freeze",
        " 且未显式指定时区（`tz`）或固定时间",
    )
    text = text.replace("calls ", "调用了 ")
    text = text.replace("uses unsynchronized concurrency/timing primitives", "使用了未同步的并发/计时原语")
    text = text.replace("uses timing sleep", "使用了固定时长等待")
    text = re.sub(r" \(via (`[^`]+`)\)", r"（经由辅助函数 \1 调用）", text)
    text = re.sub(r"（声明于 (`[^`]+`)\)", r"（声明于 \1）", text)
    return text


def _translate_warning_zh(warning):
    mapping = {
        "Fewer than two valid executions; verdict is inconclusive.": "有效执行次数少于 2 次，判定结果为无法定论（inconclusive）。",
        "Excluded/invalid observations do not contribute to the score or verdict.": "被跳过或无效的观测记录不计入稳定性得分与判定结果。",
        "Abnormal runs exist; completed outcomes contribute to scores but not suspected causes.": "存在异常运行轮次；已完成的测试结果计入得分，但不作为根因推断依据。",
        "Mixed outcomes observed, but insufficient unconfounded evidence to assign a suspected cause.": "观测到通过与失败混合出现，但缺乏无干扰的对照证据来锁定具体怀疑根因。",
    }
    if warning in mapping:
        return mapping[warning]
    if warning.startswith("Incomplete runs excluded from suspected-cause inference: "):
        rest = warning.split("Incomplete runs excluded from suspected-cause inference: ", 1)[1]
        run_ids = rest.split(". Their completed outcomes")[0]
        return f"未完成运行轮次已从根因推断中排除：{run_ids}（已完成测试仍计入得分与判定结果）。"
    if warning.startswith("No valid evidence for conditions: "):
        conds = warning.split("No valid evidence for conditions: ", 1)[1]
        return f"以下实验条件缺少有效观测证据：{conds}"
    return warning


def render_markdown(diagnosis, project_dir=None, lang="en"):
    """Render a concise, visually aligned Markdown triage report in English ('en') or Chinese ('zh')."""
    if lang not in ("en", "zh"):
        raise ValueError(f"Unsupported report language: {lang}")
    is_zh = lang == "zh"
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

    if is_zh:
        lines = [
            "# 测试稳定性诊断报告 (Test-Stability Triage Report)",
            "",
            "## 1. 概览摘要",
            "",
            f"- **分析测试总数：** {summary['tests']}",
            f"- **实验矩阵总轮数：** {summary['runs']}（共记录 {summary['records']} 条观测结果）",
            f"- **判定结果分布：** "
            f"`flaky`（不稳定）: {verdicts['flaky']} | "
            f"`stable`（稳定）: {verdicts['stable']} | "
            f"`consistently_failing`（持续失败）: {verdicts['consistently_failing']} | "
            f"`inconclusive`（证据不足）: {verdicts['inconclusive']}",
            f"- **异常 / 未完成运行轮数：** {len(abnormal_runs)}",
            f"- **稳定性计分规则：** `{policy['stability_score']}`（最少有效运行轮数：{policy.get('minimum_valid_runs', 2)}）",
            "- **结果解读说明：** 仅基于实际观测到的执行结果；怀疑根因为统计相关性，不等同于绝对根因证明。稳定性得分衡量观测到的通过频率，下方基于源码 AST 的指标为辅助消歧运行时候选根因的启发式提示。",
            "",
            "## 2. 测试诊断汇总表",
            "",
        ]
        s2_headers = [
            "测试名称", "判定结果", "稳定性得分", "重复基线 (Repeat)", "哈希种子 (Hashseed)",
            "执行顺序 (Order)", "时区扰动 (Timezone)", "单测隔离 (Isolated)", "怀疑根因", "责任归属", "判定依据",
        ]
    else:
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
            "Order", "Timezone", "Isolated", "Suspected Cause", "Fault Side", "Basis",
        ]
    s2_alignments = [
        "left", "center", "center", "center", "center",
        "center", "center", "center", "left", "center", "left",
    ]
    s2_rows = []
    any_disambiguated = False
    any_refined = False
    short_counts = Counter(_short_test_name(it["test"]["test_id"]) for it in enriched)

    for item in enriched:
        test = item["test"]
        res = item["resolution"]
        if res.get("disambiguated"):
            any_disambiguated = True
        if res.get("refined"):
            any_refined = True
        raw_short = _short_test_name(test["test_id"])
        short_name = test["test_id"] if short_counts[raw_short] > 1 else raw_short
        verdict = f"`{test['overall']['verdict']}`"
        overall_score = _fmt_score(test["overall"])
        conds = test["conditions"]
        rep_cell = _fmt_cond_cell(conds["repeat"])
        hash_cell = _fmt_cond_cell(conds["hashseed"])
        ord_cell = _fmt_cond_cell(conds["order"])
        tz_cell = _fmt_cond_cell(conds.get("tz", {}))
        iso_cell = _fmt_cond_cell(conds["isolated"])

        resolved_cands = res["resolved_candidates"]
        if resolved_cands:
            if is_zh:
                cause_str = " / ".join(
                    f"`{c}` ({_ZH_CAUSE_LABELS[c]})" if c in _ZH_CAUSE_LABELS else f"`{c}`"
                    for c in resolved_cands
                )
            else:
                cause_str = " / ".join(f"`{c}`" for c in resolved_cands)
        else:
            cause_str = "—"
        raw_side = (
            res["fault_side"]
            if resolved_cands and res["fault_side"] not in ("N/A", "Undetermined")
            else "—"
        )
        side_str = _ZH_SIDE_LABELS.get(raw_side, raw_side) if is_zh else raw_side
        raw_basis = res["basis"] if resolved_cands else "—"
        basis_str = _ZH_BASIS_LABELS.get(raw_basis, raw_basis) if is_zh else raw_basis

        s2_rows.append([
            _fmt_inline_code(short_name),
            verdict,
            overall_score,
            rep_cell,
            hash_cell,
            ord_cell,
            tz_cell,
            iso_cell,
            cause_str,
            side_str,
            basis_str,
        ])

    lines.extend(_format_markdown_table(s2_headers, s2_alignments, s2_rows))
    if any_disambiguated:
        if is_zh:
            lines.extend([
                "",
                "*\\* `AST 消歧 *`：运行时证据在 `unseeded_randomness`（未固定随机种子）与 `race_conditions`（并发时间竞态）之间存在歧义（相同种子和相同顺序下结果仍发生波动），已通过静态 AST 源码特征扫描进一步锁定。*",
            ])
        else:
            lines.extend([
                "",
                "*\\* `AST Hint *`: Runtime evidence was `ambiguous` between `unseeded_randomness` and `race_conditions` (outcomes varied under identical seed and test order); narrowed via static AST heuristic inspection.*",
            ])
    if any_refined:
        if is_zh:
            lines.extend([
                "",
                "*\\* `AST 细化 *`：运行时证据已锁定 `hash_order_sensitivity`（哈希顺序敏感）或 `shared_state_interference`（共享状态污染），结合静态 AST 源码特征进一步细化为 `float_precision`（浮点数精度断言隐患）或 `resource_leak`（文件与端口资源冲突）。*",
            ])
        else:
            lines.extend([
                "",
                "*\\* `AST Refined *`: Runtime evidence identified `hash_order_sensitivity` or `shared_state_interference` and was further refined into `float_precision` or `resource_leak` via static AST source patterns.*",
            ])

    lines.extend([
        "",
        "## 3. 诊断详情与证据溯源" if is_zh else "## 3. Diagnostic Details and Evidence Traceability",
        "",
    ])

    non_stable = [
        item for item in enriched
        if item["test"]["overall"]["verdict"] != "stable" or item["test"]["warnings"]
    ]
    if not non_stable:
        lines.append(
            "所有被分析的测试均表现为 `stable`（稳定），且无告警信息。"
            if is_zh
            else "All analysed tests were observed as `stable` with no warnings."
        )
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
                c0 = cands_key[0]
                title_label = (
                    f"`{c0}`（{_ZH_CAUSE_LABELS.get(c0, c0)}）"
                    if is_zh and c0 in _ZH_CAUSE_LABELS
                    else f"`{c0}`"
                )
            elif cands_key:
                title_label = " / ".join(f"`{c}`" for c in cands_key) + ("（存在歧义）" if is_zh else " (ambiguous)")
            else:
                title_label = "未分类 / 非波动测试与告警" if is_zh else "Unclassified / Non-Flaky & Warnings"

            group_sides = []
            for it in group_items:
                fs = it["resolution"]["fault_side"]
                if fs not in ("N/A", "Undetermined", "—"):
                    side_label = _ZH_SIDE_LABELS.get(fs, fs) if is_zh else fs
                    if side_label not in group_sides:
                        group_sides.append(side_label)
            side_heading = f" — {' / '.join(group_sides)}" if group_sides else ""
            count_label = (
                f"{len(group_items)} 个测试"
                if is_zh
                else f"{len(group_items)} test{'s' if len(group_items) != 1 else ''}"
            )
            lines.append(f"### 3.{idx} {title_label}{side_heading} ({count_label})")
            lines.append("")

            # Collect all distinct runtime (status, candidates, reason, resolution_mode) tuples across tests in this group
            distinct_reasons = []
            per_test_reason_needed = False
            reason_signatures_by_test = []
            for it in group_items:
                t_sigs = []
                for cause in it["test"]["suspected_causes"]:
                    c_mode = cause.get("resolution") or (
                        "refined"
                        if it["resolution"].get("refined")
                        else "disambiguated"
                        if it["resolution"].get("disambiguated")
                        else "unchanged"
                    )
                    rt_cands = tuple(cause.get("runtime_candidates") or cause["candidates"])
                    cur_cands = tuple(cause["candidates"])
                    sig = (
                        cause["status"],
                        rt_cands,
                        cur_cands,
                        cause["reason"],
                        c_mode,
                    )
                    t_sigs.append(sig)
                    if sig not in distinct_reasons:
                        distinct_reasons.append(sig)
                reason_signatures_by_test.append(t_sigs)
                if len(t_sigs) > 1:
                    per_test_reason_needed = True

            if len(distinct_reasons) > 1:
                per_test_reason_needed = True

            def _fmt_cause_header(status, rt_cands, cur_cands, reason, c_mode, test_id=None):
                cur_cands_fmt = " / ".join(f"`{c}`" for c in cur_cands)
                rt_plain = " / ".join(rt_cands)
                if c_mode == "refined":
                    prefix = "运行时怀疑根因" if is_zh else "Runtime suspected cause"
                    status_label = (
                        f"{rt_plain} -> 已由 AST 启发式细化"
                        if is_zh
                        else f"{rt_plain} -> refined by AST hint"
                    )
                elif c_mode == "disambiguated":
                    prefix = "运行时怀疑根因" if is_zh else "Runtime Suspicion"
                    status_label = (
                        "`ambiguous` -> 已由 AST 启发式提示消歧"
                        if is_zh
                        else "`ambiguous` -> narrowed by AST heuristic hint"
                    )
                else:
                    prefix = "运行时怀疑根因" if is_zh else "Runtime Suspicion"
                    status_label = f"`{status}`"
                reason_txt = _translate_reason_zh(reason) if is_zh else reason
                if test_id is not None:
                    return (
                        f"> - **{prefix} ({_fmt_inline_code(test_id)}, {status_label}):** "
                        f"{cur_cands_fmt} — {reason_txt}"
                    )
                return f"> - **{prefix} ({status_label}):** {cur_cands_fmt} — {reason_txt}"

            if distinct_reasons and not per_test_reason_needed:
                status, rt_cands, cur_cands, reason, c_mode = distinct_reasons[0]
                lines.append(_fmt_cause_header(status, rt_cands, cur_cands, reason, c_mode))
            elif distinct_reasons:
                for it in group_items:
                    t = it["test"]
                    for cause in t["suspected_causes"]:
                        c_mode = cause.get("resolution") or (
                            "refined"
                            if it["resolution"].get("refined")
                            else "disambiguated"
                            if it["resolution"].get("disambiguated")
                            else "unchanged"
                        )
                        rt_cands = tuple(cause.get("runtime_candidates") or cause["candidates"])
                        cur_cands = tuple(cause["candidates"])
                        lines.append(
                            _fmt_cause_header(
                                cause["status"],
                                rt_cands,
                                cur_cands,
                                cause["reason"],
                                c_mode,
                                test_id=t["test_id"],
                            )
                        )
            else:
                lines.append(
                    "> - **运行时怀疑根因:** 未从运行时实验条件中分配根因。"
                    if is_zh
                    else "> - **Runtime Suspicion:** None assigned from runtime condition evidence."
                )
            lines.append("")

            s3_headers = (
                ["测试名称", "稳定性得分", "失败率 / 错误率", "支撑运行轮次（日志）", "源码 AST 启发式提示"]
                if is_zh
                else ["Test", "Stability", "Fail / Err", "Supporting Runs (Logs)", "Source Heuristic Hints"]
            )
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
                    translated = [_translate_hint_zh(h) if is_zh else h for h in combined_hints]
                    hint_cell = _escape_table_cell("; ".join(translated))
                elif t["suspected_causes"]:
                    if not s_info.get("available"):
                        hint_cell = "源码不可用，无法执行 AST 检查" if is_zh else "Source not available for AST check"
                    else:
                        hint_cell = "未匹配到已知 AST 缺陷模式" if is_zh else "No matching AST pattern"
                elif ov["verdict"] == "flaky":
                    # Flaky test with no runtime cause assigned: surface any AST hints if source is available
                    unmatched = []
                    for cat_name, hits in (s_info.get("indicators") or {}).items():
                        for hit in hits:
                            raw_u = (
                                f"AST hint (no runtime cause): `{hit['location']}` {hit['detail']} "
                                f"(`{cat_name}`?, {hit['side']})"
                            )
                            unmatched.append(_translate_hint_zh(raw_u) if is_zh else raw_u)
                    if unmatched:
                        hint_cell = _escape_table_cell("; ".join(unmatched))
                    elif not s_info.get("available"):
                        hint_cell = "源码不可用，无法执行 AST 检查" if is_zh else "Source not available for AST check"
                    else:
                        hint_cell = "未匹配到已知 AST 缺陷模式" if is_zh else "No matching AST pattern"
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
                    group_warnings.append((t["test_id"], _translate_warning_zh(w) if is_zh else w))

            lines.extend(_format_markdown_table(s3_headers, s3_alignments, s3_rows))
            if group_warnings:
                lines.append("")
                warn_label = "告警" if is_zh else "Warning"
                for t_id, w_text in group_warnings:
                    lines.append(f"- **{warn_label} ({_fmt_inline_code(t_id)}):** {w_text}")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"
