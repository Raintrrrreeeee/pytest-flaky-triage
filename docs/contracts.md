# Stage 1 / Stage 2 / Stage 3 Data Contracts

This document defines the JSON schemas and architectural contract boundaries between:
- **Stage 1 (`runner`)**: Controlled multi-perturbation test execution (`records.jsonl`)
- **Stage 2 (`src/analysis`)**: Pure runtime statistical causal attribution (`diagnosis.json`)
- **Stage 3 (`src/report`)**: Static AST heuristic disambiguation, category refinement, and bilingual report generation (`report.md`)

---

## 1. Stage 1 Output Contract (`records.jsonl`)

Each line in `records.jsonl` is a JSON object with `"record_type"` set to either `"run"` or `"result"`:

### 1.1 `"run"` Record
```json
{
  "schema_version": 1,
  "record_type": "run",
  "run_id": 1,
  "condition": "repeat",
  "condition_value": "1",
  "seed": 42,
  "pythonhashseed": "0",
  "order": "default",
  "tz": "UTC",
  "exit_code": 0,
  "status": "ok",
  "tests_expected": 8,
  "tests_reported": 8,
  "duration_ms": 120,
  "started_at": "2026-01-01T00:00:00Z",
  "finished_at": "2026-01-01T00:00:01Z",
  "selected_test": null,
  "error_message": null
}
```
- `condition`: One of `"repeat"`, `"hashseed"`, `"order"`, `"tz"`, `"isolated"`.
- `status`: One of `"ok"`, `"failed"`, `"timeout"`, `"error"`, `"incomplete"`.

### 1.2 `"result"` Record
```json
{
  "schema_version": 1,
  "record_type": "result",
  "run_id": 1,
  "test_id": "tests/test_demo.py::test_example",
  "condition": "repeat",
  "condition_value": "1",
  "position": 1,
  "outcome": "passed",
  "duration_ms": 5,
  "message": null
}
```
- `outcome`: One of `"passed"`, `"failed"`, `"error"`, `"skipped"`, `"xfailed"`, `"xpassed"`, `"timeout"`, `"not_run"`.

---

## 2. Stage 2 Output Contract (`diagnosis.json`)

Stage 2 (`src.analysis`) performs **pure runtime statistical attribution** from `records.jsonl` without inspecting Python source files.

```json
{
  "schema_version": 1,
  "policy": {
    "stability_score": "passed / (passed + failed + error)",
    "valid_outcomes": ["error", "failed", "passed"],
    "minimum_valid_runs": 2,
    "scope": "Observed executions only; suspected causes are correlations, not proven root causes."
  },
  "summary": {
    "tests": 8,
    "runs": 28,
    "records": 197,
    "verdicts": {
      "stable": 1,
      "consistently_failing": 0,
      "flaky": 7,
      "inconclusive": 0
    }
  },
  "runs": [ ... ],
  "tests": [
    {
      "test_id": "tests/test_demo.py::test_example",
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
          "reason": "Outcomes differ across test orders, or isolated executions pass while suite executions fail, with a consistent repeat baseline. Suspected polluting predecessor(s): test_polluter.",
          "run_ids": [1, 2, 19, 20],
          "suspected_polluters": ["tests/test_demo.py::test_polluter"]
        }
      ],
      "warnings": [],
      "evidence": [ ... ]
    }
  ]
}
```

---

## 3. Root-Cause Taxonomy (All 8 Cause Categories) and Stage 2 vs. Stage 3 Boundary

The system supports **8 root-cause categories** (`ALL_CAUSE_CATEGORIES`), partitioned across Stage 2 (runtime perturbation evidence) and Stage 3 (static AST heuristic evidence):

| Category | Stage 2 (Runtime Perturbation) | Stage 3 (AST Disambiguation / Refinement) | Description |
| :--- | :--- | :--- | :--- |
| `unseeded_randomness` | Emitted as `["unseeded_randomness", "race_conditions"]` (`status: "ambiguous"`) when outcomes vary under identical seed/order (`repeat`) or rare/multi-perturbation flips | **Disambiguated** (`resolution: "disambiguated"`) when AST finds unseeded PRNG calls (`random.*`, `uuid.uuid4`, `np.random.*`, `os.urandom`, `secrets.*`) | Unseeded pseudo-random number generation |
| `race_conditions` | Emitted as `["unseeded_randomness", "race_conditions"]` (`status: "ambiguous"`) when outcomes vary under identical seed/order (`repeat`) or rare/multi-perturbation flips | **Disambiguated** (`resolution: "disambiguated"`) when AST finds concurrency/timing primitives (`threading`, `asyncio`, `multiprocessing`, `time.sleep`, wall-clock deltas) | Concurrency race or wall-clock timing sensitivity |
| `hash_order_sensitivity` | Emitted as `["hash_order_sensitivity"]` (`status: "suspected"`) when outcomes flip across `PYTHONHASHSEED` values with a consistent repeat baseline | Preserved as `["hash_order_sensitivity"]` when AST finds unordered set/dict order usage (`Runtime+AST`), or **refined** to `float_precision` | Dependence on non-deterministic `set`/`dict` iteration order |
| `shared_state_interference` | Emitted as `["shared_state_interference"]` (`status: "suspected"`) when `order` or `isolated` perturbations flip outcomes (including predecessor polluter intersection via `suspected_polluters`) | Preserved as `["shared_state_interference"]` when AST finds in-memory mutable globals/singletons/env vars/DB state, or **refined** to `resource_leak` | Cross-test or self-inflicted shared state pollution |
| `timezone_sensitivity` | Emitted as `["timezone_sensitivity"]` (`status: "suspected"`) when `TZ` perturbations flip outcomes | Confirmed (`Runtime+AST`) when AST finds naive `datetime.now()` / `fromtimestamp()` / `time.localtime()` / `mktime()` | Implicit dependence on host local timezone |
| `resource_leak` | Represented at runtime as `["shared_state_interference"]` or `["unseeded_randomness", "race_conditions"]` | **Refined** (`resolution: "refined"`) from `shared_state_interference` or **disambiguated** (`resolution: "disambiguated"`) from `["unseeded_randomness", "race_conditions"]` when AST finds unisolated hardcoded file paths or fixed network ports | Unisolated filesystem path or fixed port contention |
| `external_io` | Represented at runtime as `["unseeded_randomness", "race_conditions"]` (`status: "ambiguous"`) | **Disambiguated** (`resolution: "disambiguated"`) from `["unseeded_randomness", "race_conditions"]` when AST finds unmocked network/HTTP/socket/DNS calls (`urllib`, `requests`, `httpx`, `aiohttp`, `socket`) | Unmocked external network or service I/O |
| `float_precision` | Represented at runtime as `["hash_order_sensitivity"]` (`status: "suspected"`) or `["unseeded_randomness", "race_conditions"]` (`status: "ambiguous"`) | **Refined** (`resolution: "refined"`) from `hash_order_sensitivity` or **disambiguated** (`resolution: "disambiguated"`) from `["unseeded_randomness", "race_conditions"]` when AST finds strict float equality (`==`) over non-associative reductions (`sum()`, accumulation loops) without `math.isclose` / `pytest.approx` | Non-associative floating-point reduction order with strict equality assertions |

---

## 4. Stage 3 Enrichment Contract (`disambiguate_causes`)

When Stage 3 (`src.report.heuristics.disambiguate_causes`) processes a test's `suspected_causes` alongside AST `source_info`, it distinguishes **Ambiguity Resolution (`disambiguated`)** from **Category Refinement (`refined`)** and preserves the original Stage 2 runtime fields:

### 4.1 Per-Cause Enrichment Fields (`test["suspected_causes"][i]`)
Whenever Stage 3 narrows or refines `cause["candidates"]`:
- `runtime_candidates`: Original Stage 2 candidate list (e.g. `["unseeded_randomness", "race_conditions"]`, `["hash_order_sensitivity"]`, or `["shared_state_interference"]`).
- `runtime_status`: Original Stage 2 status (`"ambiguous"` or `"suspected"`).
- `runtime_reason`: Original Stage 2 reason string.
- `candidates`: Updated Stage 3 resolved candidate list (e.g. `["external_io"]`, `["float_precision"]`, `["resource_leak"]`).
- `status`: Updated status (`"suspected"` when narrowed to 1 candidate).
- `reason`: Updated English reason string matching the resolved category (preserving any `Suspected polluting predecessor(s): ...` suffix).
- `resolution`:
  - `"disambiguated"`: Runtime had multiple ambiguous candidates (`["unseeded_randomness", "race_conditions"]`) and AST narrowed them to a specific candidate (`unseeded_randomness`, `race_conditions`, `external_io`, `resource_leak`, or `float_precision`).
  - `"refined"`: Runtime already identified a single deterministic mechanism (`hash_order_sensitivity` or `shared_state_interference`) and AST refined it to a more specific sub-category (`float_precision` or `resource_leak`).
- `disambiguated`: `True` iff `resolution == "disambiguated"`.
- `refined`: `True` iff `resolution == "refined"`.

### 4.2 Summary Resolution Dictionary (`disambiguate_causes` return value)
```json
{
  "resolved_candidates": ["float_precision"],
  "runtime_candidates": ["hash_order_sensitivity"],
  "fault_side": "Test",
  "heuristic_notes": [ ... ],
  "compact_hints": [ ... ],
  "conflicting_hints": [],
  "basis": "AST Refined *",
  "resolution": "refined",
  "disambiguated": false,
  "refined": true
}
```
- `basis`:
  - `"AST Hint *"` (ZH: `"AST 消歧 *"`): Runtime ambiguity (`unseeded_randomness` vs `race_conditions`) was narrowed via AST heuristics (`disambiguated == True`).
  - `"AST Refined *"` (ZH: `"AST 细化 *"`): Single runtime candidate (`hash_order_sensitivity` or `shared_state_interference`) was refined into `float_precision` or `resource_leak` (`refined == True`).
  - `"Runtime+AST"` (ZH: `"运行时+AST"`): Runtime single candidate confirmed by matching AST pattern.
  - `"Runtime"` (ZH: `"仅运行时"`): Runtime single candidate with no AST match.
  - `"Ambiguous"` (ZH: `"存在歧义"`): Runtime ambiguity remains unresolved.
  - `"Runtime (AST conflict)"` (ZH: `"运行时 (AST 冲突)"`): AST only matched categories incompatible with runtime evidence; runtime candidate is preserved.
