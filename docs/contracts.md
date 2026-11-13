# Stage 1 / Stage 2 / Stage 3 Data Contracts

This document defines the CSV/JSON schemas and architectural contract boundaries between:
- **Stage 1 (`flaky_triage.runner` / `scripts/detect.sh` & `flaky_plugin.py`)**: Controlled 5-dimension perturbation execution (`events/run_XXXX.jsonl` -> `results.csv` & `run_meta.csv`)
- **Stage 2 (`src/analysis`)**: Pure runtime statistical scoring, causal attribution, and polluter inference (`diagnosis.json`)
- **Stage 3 (`src/report`)**: Static AST heuristic disambiguation, category refinement, fault-side attribution, and bilingual Markdown report generation (`report.md`)

---

## 1. Stage 1 -> Stage 2 Contract (`events/run_XXXX.jsonl`, `results.csv`, `run_meta.csv`)

### 1.1 Sideband Event Stream (`events/run_XXXX.jsonl`)
During each `pytest` invocation, the built-in sideband hook plugin (`flaky_plugin.py`) writes structured per-run lifecycle events to `events/run_XXXX.jsonl` (with an AWK/regex log-parser fallback when a process crashes before flushing hooks):
- **`start` event**: Emitted during `pytest_collection_modifyitems` after applying in-memory `O(N)` test reordering; records the run start state and collected test count (`collected` / `tests_expected`).
- **`report` event**: Emitted across `pytest_runtest_logreport` (`setup`, `call`, `teardown` phases); records each test's execution position (`position`), node ID (`test_id`), and resolved test outcome (`outcome`).
- **`finish` event**: Emitted during `pytest_sessionfinish`; records the session exit status (`pytest_exit` / `exit_code`), total reported test count (`parsed` / `tests_reported`), and elapsed wall-clock duration.

After each run completes (or is terminated by the process-group timeout controller with exit code `124`), Stage 1 aggregates `events/run_XXXX.jsonl` against the master `collected.txt` list (marking any unreached tests as `timeout` or `not_run`) and appends rows to `results.csv` and `run_meta.csv`.

### 1.2 Per-Test Observation Table (`results.csv`)
- **Logical / Contract Columns**: `run_id,condition,seed,order_seed,test_id,outcome`
- **Serialized CSV Header (`RESULT_FIELDS`)**: `run_id,condition,condition_value,position,test_id,outcome`
  - `run_id`: 1-based integer identifier of the matrix run.
  - `condition`: Perturbation dimension enum — one of `repeat`, `hashseed`, `order`, `tz`, `isolated`.
  - `condition_value` (encodes `seed` / `order_seed` / `tz` / isolated target): Perturbation parameter for the run (e.g., repeat index, `PYTHONHASHSEED` seed, `reverse` / `shuffle:<order_seed>`, timezone name, or `alone`).
  - `position`: 1-based execution order index within the run (empty when `timeout` or `not_run`).
  - `test_id`: Full pytest node ID (e.g., `tests/test_cart.py::test_taxes_format`).
  - `outcome`: Test result enum — primary outcomes are `passed`, `failed`, `error`, `skipped` (extended outcomes: `xfailed`, `xpassed`, `timeout`, `not_run`).

### 1.3 Per-Run Metadata Table (`run_meta.csv`)
- **Logical / Contract Columns**: `run_id,condition,seed,order_seed,tz,pytest_exit,collected,parsed,log_file`
- **Serialized CSV Header (`META_FIELDS`)**: `run_id,condition,condition_value,hash_seed,exit_code,status,seconds,tests_expected,tests_reported,log_path`
  - `run_id`: 1-based integer run identifier matching `results.csv`.
  - `condition`: One of `repeat`, `hashseed`, `order`, `tz`, `isolated`.
  - `condition_value` / `seed` / `order_seed` / `tz`: Active perturbation value for the run.
  - `hash_seed` (`seed`): Integer `PYTHONHASHSEED` value used for the run.
  - `exit_code` (`pytest_exit`): Process exit code (`0` all passed, `1` test failures, `124` timeout, or signal exit code).
  - `status`: Run completion status — one of `ok`, `timeout`, `crashed`, `empty`.
  - `seconds`: Elapsed run time in seconds.
  - `tests_expected` (`collected`): Number of tests expected in the run (`N` for suite runs, `1` for `isolated` runs).
  - `tests_reported` (`parsed`): Number of tests that produced a concrete outcome before the process exited.
  - `log_path` (`log_file`): Relative path to the raw pytest log file (e.g., `logs/run_001.log`).

---

## 2. Stage 2 -> Stage 3 Contract (`diagnosis.json`)

Stage 2 (`python -m analysis`) performs pure runtime statistical scoring and causal attribution from `results.csv` and `run_meta.csv` without inspecting Python source files.

### 2.1 Top-Level Fields
- `schema_version`: Integer schema version (`1`).
- `generated_at`: Timestamp / generation metadata (when emitted by pipeline wrappers) alongside `policy` and `runs`.
- `policy`: Scoring formulas (`stability_score = passed / (passed + failed + error)`), denominator exclusions, and minimum valid run threshold (`minimum_valid_runs: 2`).
- `summary`: Suite-level counts and verdict distribution:
  - Conceptual fields: `total_tests`, `flaky_tests`, `deterministic_failures`, `passing_tests`, `Other`.
  - Serialized fields: `tests`, `runs`, `records`, and `verdicts` (`stable`, `consistently_failing`, `flaky`, `inconclusive`).
- `runs`: Array of validated run metadata objects from `run_meta.csv`.
- `tests`: Array of per-test diagnostic objects.

### 2.2 Per-Test Object (`tests[]`)
Each element in `tests[]` contains:
- `test_id`: Full pytest node ID string.
- `classification` / `overall`:
  - Conceptual classification enum: `flaky`, `deterministic_failure`, `passing`, `Other`.
  - Serialized `overall` block: `total`, `valid`, `counts`, `excluded`, `invalid`, `stability_score`, `failure_frequency`, `error_frequency`, and `verdict` (`flaky`, `consistently_failing`, `stable`, `inconclusive`).
- `pass_rate_by_condition` / `conditions`: Per-condition summary blocks keyed by `repeat`, `hashseed`, `order`, `tz`, and `isolated`.
- `suspected_causes`: Array of runtime cause attribution objects (empty when verdict is not `flaky`).
- `evidence_runs` / `evidence`: Array of per-run observations (`{"run_id": int, "position": int | null, "outcome": str}`).
- `notes` / `warnings`: Array of diagnostic warning strings (e.g., excluded incomplete or abnormal runs, insufficient valid runs).

### 2.3 Suspected Cause Object (`suspected_causes[]`)
Each element in `suspected_causes[]` contains:
- `candidates`: Non-empty list of root-cause category identifiers. All **9 legal cause categories** supported across Stage 2 and Stage 3 (`src/report/core.py`):
  1. `order_dependency`
  2. `shared_state_interference`
  3. `hash_seed_sensitivity` (canonical alias: `hash_order_sensitivity`)
  4. `unseeded_randomness`
  5. `race_conditions`
  6. `timezone_sensitivity`
  7. `resource_leak`
  8. `external_io`
  9. `float_precision`
- `confidence`: Attribution confidence level (`high`, `medium`, `low`).
- `status`: Attribution status enum (`confirmed`, `ambiguous`, `tentative`, or `suspected` in Stage 2/3 JSON serialization).
- `reason`: Human-readable English explanation of the runtime or AST-refined evidence.
- `run_ids`: Sorted list of integer `run_id` values supporting this cause.
- `suspected_polluters` (optional, `string[]`): Present on order/shared-state causes when predecessor set intersection (`common_on_fail - ever_on_pass`) identifies specific polluting predecessor test IDs.

```json
{
  "schema_version": 1,
  "policy": {
    "stability_score": "passed / (passed + failed + error)",
    "minimum_valid_runs": 2,
    "scope": "Scores and verdicts describe observed outcomes across the configured runs."
  },
  "summary": {
    "tests": 22,
    "runs": 28,
    "records": 418,
    "verdicts": {
      "stable": 2,
      "consistently_failing": 0,
      "flaky": 20,
      "inconclusive": 0
    }
  },
  "runs": [ ... ],
  "tests": [
    {
      "test_id": "tests/test_cart.py::test_shared_cart_has_no_prior_laptop",
      "overall": { ... },
      "conditions": {
        "repeat": { ... },
        "hashseed": { ... },
        "order": { ... },
        "tz": { ... },
        "isolated": { ... }
      },
      "suspected_causes": [
        {
          "candidates": ["shared_state_interference"],
          "status": "suspected",
          "reason": "Outcomes differ across test orders, or isolated executions pass while suite executions fail, with a consistent repeat baseline. Suspected polluting predecessor(s): test_new_cart_is_empty.",
          "run_ids": [1, 2, 19, 28],
          "suspected_polluters": ["tests/test_cart.py::test_new_cart_is_empty"]
        }
      ],
      "warnings": [],
      "evidence": [ ... ]
    }
  ]
}
```

---

## 3. Stage 3 Disambiguation & Report Contract (`report.md` & Enriched `diagnosis.json`)

When Stage 3 (`python -m report` / `src.report.heuristics.disambiguate_causes`) inspects the target project's Python AST (following same-file helpers, `conftest.py` fixtures, `setup_method`, and up to 3 hops of cross-file imports), it enriches `suspected_causes` while preserving the original Stage 2 runtime fields.

### 3.1 Preserved Runtime & Resolution Fields on `suspected_causes[]`
Whenever Stage 3 narrows an ambiguous cause or refines a deterministic cause:
- `runtime_candidates`: Original Stage 2 candidate list before AST analysis (e.g., `["unseeded_randomness", "race_conditions"]`, `["hash_order_sensitivity"]`, or `["shared_state_interference"]`).
- `runtime_status`: Original Stage 2 status string (`"ambiguous"` or `"suspected"`).
- `runtime_reason`: Original Stage 2 runtime reason string.
- `resolution`:
  - `"disambiguated"`: Runtime emitted multiple ambiguous candidates (`["unseeded_randomness", "race_conditions"]`) and AST narrowed them to a single category (`unseeded_randomness`, `race_conditions`, `external_io`, `resource_leak`, or `float_precision`).
  - `"refined"`: Runtime identified a single mechanism (`hash_order_sensitivity` / `hash_seed_sensitivity` or `shared_state_interference` / `order_dependency`) and AST refined it to a more specific root cause (`float_precision` or `resource_leak`).
- `disambiguated`: Boolean (`True` iff `resolution == "disambiguated"`).
- `refined`: Boolean (`True` iff `resolution == "refined"`).

### 3.2 AST Fault-Side Attribution (`fault_side`)
Stage 3 assigns fault ownership according to where the AST indicators were detected across the test and production call graph. The valid `fault_side` enum values are:
- `"Test Side"`: Indicators appear only in the test function, same-file test helpers, test class `setup_method` / `setUp`, or `conftest.py` fixtures.
- `"Code Side"`: Indicators appear only in imported production modules under `src/` or application packages.
- `"Both"`: Matching indicators appear in both test-side code and production-side code (`"Test Side"` and `"Code Side"`).
- `"Undetermined"`: Source files are unavailable, unparseable, or contain no matching AST indicators for the resolved cause.

```json
{
  "resolved_candidates": ["float_precision"],
  "runtime_candidates": ["hash_order_sensitivity"],
  "fault_side": "Test Side",
  "heuristic_notes": [ ... ],
  "compact_hints": [ ... ],
  "conflicting_hints": [],
  "basis": "AST Refined *",
  "resolution": "refined",
  "disambiguated": false,
  "refined": true
}
```
