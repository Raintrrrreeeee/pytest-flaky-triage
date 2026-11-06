"""Read and validate the runner's CSV execution-record contract."""

import csv
import math
from collections import defaultdict
from pathlib import Path

CONDITIONS = ("repeat", "hashseed", "order", "isolated")
OUTCOMES = ("passed", "failed", "error", "skipped", "xfailed", "xpassed", "timeout", "not_run")
RESULT_FIELDS = ("run_id", "condition", "condition_value", "position", "test_id", "outcome")
META_FIELDS = (
    "run_id", "condition", "condition_value", "hash_seed", "exit_code", "status",
    "seconds", "tests_expected", "tests_reported", "log_path",
)
STATUSES = {"ok", "timeout", "interrupted", "internal_error", "usage_error", "no_tests", "crashed"}


class InputError(ValueError):
    """An input violates the agreed execution-record contract."""


def _read(path, fields):
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, strict=True)
        if reader.fieldnames is None or len(reader.fieldnames) != len(fields) or set(reader.fieldnames) != set(fields):
            raise InputError(f"{path}: expected columns {','.join(fields)}")
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise InputError(f"{path}:{reader.line_num}: wrong number of fields")
            yield row


def _integer(value, field, minimum=0):
    try:
        number = int(value)
    except ValueError as exc:
        raise InputError(f"{field}: expected integer, got {value!r}") from exc
    if number < minimum:
        raise InputError(f"{field}: must be >= {minimum}")
    return number


def _condition(row):
    condition, value = row["condition"], row["condition_value"]
    if condition not in CONDITIONS or not value:
        raise InputError(f"run {row['run_id']}: invalid condition/value")
    if condition in ("repeat", "hashseed"):
        _integer(value, "condition_value", 1 if condition == "repeat" else 0)
    elif condition == "isolated" and value != "alone":
        raise InputError("isolated condition_value must be alone")
    elif condition == "order" and value != "reverse":
        if not value.startswith("shuffle:"):
            raise InputError("order condition_value must be reverse or shuffle:N")
        _integer(value.removeprefix("shuffle:"), "shuffle seed")


def load_records(results_path, meta_path=None):
    """Return (result rows, metadata keyed by integer run ID).

    Metadata is required and defaults to run_meta.csv beside results_path.
    Unknown/duplicate outcomes and incomplete joins are errors, not lost evidence.
    """
    results_path = Path(results_path)
    meta_path = Path(meta_path) if meta_path else results_path.with_name("run_meta.csv")
    runs = {}
    for row in _read(meta_path, META_FIELDS):
        _condition(row)
        for field in ("run_id", "hash_seed", "exit_code", "tests_expected", "tests_reported"):
            min_val = 1 if field == "run_id" else (-255 if field == "exit_code" else 0)
            row[field] = _integer(row[field], field, min_val)
        run_id = row["run_id"]
        if run_id in runs:
            raise InputError(f"duplicate metadata run_id {run_id}")
        if row["status"] not in STATUSES:
            raise InputError(f"run {run_id}: unknown status {row['status']!r}")
        expected_status = {0: "ok", 1: "ok", 2: "interrupted", 3: "internal_error", 4: "usage_error", 5: "no_tests", 124: "timeout"}.get(row["exit_code"], "crashed")
        if row["status"] != expected_status:
            raise InputError(f"run {run_id}: status disagrees with exit_code")
        expected_seed = int(row["condition_value"]) if row["condition"] == "hashseed" else 0
        if row["hash_seed"] != expected_seed:
            raise InputError(f"run {run_id}: hash_seed disagrees with condition")
        try:
            row["seconds"] = float(row["seconds"]) if row["seconds"] else None
        except ValueError as exc:
            raise InputError(f"run {run_id}: invalid seconds") from exc
        if row["seconds"] is not None and (not math.isfinite(row["seconds"]) or row["seconds"] < 0):
            raise InputError(f"run {run_id}: seconds must be finite and nonnegative")
        if not row["log_path"]:
            raise InputError(f"run {run_id}: missing log_path")
        runs[run_id] = row

    rows, seen, by_run = [], set(), defaultdict(list)
    for row in _read(results_path, RESULT_FIELDS):
        _condition(row)
        row["run_id"] = _integer(row["run_id"], "run_id", 1)
        row["position"] = _integer(row["position"], "position", 1) if row["position"] else None
        run_id = row["run_id"]
        if not row["test_id"].strip() or row["outcome"] not in OUTCOMES:
            raise InputError(f"run {run_id}: missing test_id or unknown outcome")
        key = (run_id, row["test_id"])
        if key in seen:
            raise InputError(f"duplicate test/run record {key!r}")
        seen.add(key)
        if run_id not in runs:
            raise InputError(f"run {run_id}: missing metadata")
        if any(row[field] != runs[run_id][field] for field in ("condition", "condition_value")):
            raise InputError(f"run {run_id}: result and metadata conditions disagree")
        rows.append(row)
        by_run[run_id].append(row)

    if not rows:
        raise InputError("results.csv contains no execution records")
    suite_ids = None
    for run_id, meta in runs.items():
        records = by_run[run_id]
        reported = sum(r["outcome"] not in ("timeout", "not_run") for r in records)
        if len(records) != meta["tests_expected"] or reported != meta["tests_reported"]:
            raise InputError(f"run {run_id}: result counts disagree with metadata")
        positions = [r["position"] for r in records if r["position"] is not None]
        if len(set(positions)) != len(positions) or any(p > len(records) for p in positions):
            raise InputError(f"run {run_id}: duplicate or out-of-range positions")
        if meta["condition"] == "isolated":
            if len(records) != 1:
                raise InputError(f"run {run_id}: isolated run must expect one test")
        elif meta["status"] == "ok" and records:
            ids = {r["test_id"] for r in records}
            if suite_ids is not None and ids != suite_ids:
                raise InputError(f"run {run_id}: full-suite test IDs changed within the matrix")
            suite_ids = ids
    if suite_ids is not None and any(r["test_id"] not in suite_ids for r in rows):
        raise InputError("isolated test ID is absent from full-suite runs")
    return rows, runs
