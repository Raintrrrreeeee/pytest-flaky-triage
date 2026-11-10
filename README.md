# pytest-flaky-triage

[![CI](https://github.com/Raintrrrreeeee/pytest-flaky-triage/actions/workflows/ci.yml/badge.svg)](https://github.com/Raintrrrreeeee/pytest-flaky-triage/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/Raintrrrreeeee/pytest-flaky-triage)](https://github.com/Raintrrrreeeee/pytest-flaky-triage/releases)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![pytest >= 7.4](https://img.shields.io/badge/pytest-%3E%3D7.4-0A9EDC.svg)](https://docs.pytest.org/)
[![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey.svg)](#)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

**English** | [简体中文](README.zh-CN.md)

A three-stage flaky-test detection, statistical root-cause inference, and static AST fault-side triage tool for Python `pytest` suites.

Unlike simple rerun plugins (`pytest-repeat`, `pytest-flakefinder`, `pytest-random-order`) that only tell you *whether* a test failed across repeated runs, **`pytest-flaky-triage`** runs controlled single-factor experiments, identifies the exact polluting predecessor test for order-dependent failures, and combines runtime evidence with static Python AST analysis to tell you **why** a test is flaky and **whether the bug lives in the test (`Test Side`) or in production code (`Code Side`)**.

```text
+-------------------------------------------------------------------------+
| Stage 1: Controlled Perturbation Matrix (scripts/detect.sh)             |
|   repeat -> hashseed -> order (O(N) hook) -> tz -> isolated             |
|   Pytest Sideband Hook Plugin + Process-Group Timeout Controller        |
+------------------------------------+------------------------------------+
                                     |  results.csv & run_meta.csv
                                     v
+-------------------------------------------------------------------------+
| Stage 2: Statistical & Causal Inference (src/analysis/)                 |
|   Crash-resilient stability score + Orthogonal condition attribution    |
|   Bidirectional isolation check + Polluter set intersection             |
+------------------------------------+------------------------------------+
                                     |  diagnosis.json
                                     v
+-------------------------------------------------------------------------+
| Stage 3: Static Python AST Disambiguation & Report (src/report/)        |
|   3-hop cross-file call graph + conftest.py fixtures + setup_method     |
|   Test Side vs. Code Side fault attribution -> report.md (EN / ZH)      |
+-------------------------------------------------------------------------+
```

---

## Sample Output Preview (`out/report.md`)

```markdown
| Test                                     | Verdict  | Score        | Repeat | Hashseed | Order | Timezone | Isolated | Suspected Cause             | Fault Side | Basis       |
| :--------------------------------------- | :------: | :----------: | :----: | :------: | :---: | :------: | :------: | :-------------------------- | :--------: | :---------- |
| `test_taxes_format`                      | `flaky`  | 0.14 (4/28)  |  0/10  |   4/8    |  0/6  |   0/3    |   0/1    | `hash_order_sensitivity`    | Test Side  | Runtime+AST |
| `test_shared_cart_has_no_prior_laptop`   | `flaky`  | 0.14 (4/28)  |  0/10  |   0/8    |  3/6  |   0/3    |   1/1    | `shared_state_interference` | Test Side  | Runtime+AST |
| `test_daily_order_prefix_uses_utc_date`  | `flaky`  | 0.93 (26/28) | 10/10  |   8/8    |  6/6  |   1/3    |   1/1    | `timezone_sensitivity`      | Code Side  | Runtime+AST |
| `test_receipt_timestamp_formatting`      | `flaky`  | 0.93 (26/28) | 10/10  |   8/8    |  6/6  |   1/3    |   1/1    | `timezone_sensitivity`      | Test Side  | Runtime+AST |
| `test_get_daily_quote`                   | `flaky`  | 0.25 (7/28)  |  3/10  |   2/8    |  1/6  |   1/3    |   0/1    | `unseeded_randomness`       | Code Side  | AST Hint *  |
| `test_checkout_deducts_mouse`            | `flaky`  | 0.46 (13/28) |  5/10  |   4/8    |  2/6  |   1/3    |   1/1    | `race_conditions`           | Code Side  | AST Hint *  |
| `test_add_item_to_cart_is_deterministic` | `stable` | 1.00 (28/28) | 10/10  |   8/8    |  6/6  |   3/3    |   1/1    | —                           |     —      | —           |
```

Each diagnostic section further pinpoints the **polluting predecessor test** and **exact source line**:
- **`shared_state_interference`**: `Suspected polluting predecessor(s): test_new_cart_is_empty` -> `tests/test_cart.py:120 accesses/mutates shared module-level object shared_test_cart (declared at tests/test_cart.py:10)`
- **`resource_leak`**: `src/storage/repo.py:8 operates on hardcoded file/database path via Path.write_text(...), sqlite3.connect(...) without tmp_path or tempfile`
- **`external_io`**: `src/net/client.py:5 performs unmocked external network I/O via requests.get(...)`
- **`float_precision`**: `tests/test_float.py:8 asserts strict equality (==/!=) on floating-point values without pytest.approx or math.isclose`
- **`timezone_sensitivity`**: `src/ecommerce/cart.py:82 calls datetime.now() without explicit timezone (tz) or time freeze`
- **`unseeded_randomness`**: `src/ecommerce/cart.py:57 calls random.choice(...) without a fixed seed`
- **`race_conditions`**: `src/ecommerce/cart.py:67, 71, 75 uses unsynchronized concurrency/timing primitives (time.sleep, unjoined threading.Thread)`

---

## Key Features

### Stage 1 — Controlled 5-Dimension Perturbation Runner (`flaky_triage.runner`, `scripts/detect.sh` & `flaky_plugin.py`)
- **Single-Factor Experimental Matrix**: Evaluates tests across five orthogonal conditions (`repeat`, `hashseed`, `order`, `tz`, and `isolated`), holding all other variables fixed so each perturbation isolates a distinct class of flakiness.
- **Timezone Perturbation (`tz`)**: Runs tests across extreme UTC offsets (`UTC`, `Pacific/Kiritimati` at UTC+14, and `America/Pago_Pago` at UTC-11, spanning a 25-hour window) and invokes `time.tzset()` inside the pytest hook plugin to expose naive local-time and date-boundary assumptions at any hour of the day.
- **Sideband Pytest Hook Plugin**: Captures per-test outcomes (`setup` / `call` / `teardown`) directly from pytest hooks into a structured sideband event stream, remaining accurate regardless of project `pytest.ini` flags (`-q`, custom progress bars, multiline failure tracebacks, or parameterized IDs containing spaces and brackets), with a hardened AWK/regex log-parser fallback.
- **In-Memory `O(N)` Reordering & Plugin Neutralization**: Reorders tests in-place via `pytest_collection_modifyitems` and automatically disables conflicting third-party plugins (`pytest-randomly`, `pytest-xdist`, `pytest-sugar`, `pytest-cov`, `pytest-rerunfailures`, `pytest-order`) during matrix runs.
- **Cross-Platform Process-Group Timeout & Hang Protection**: Executes every run in an isolated process group with `stdin` closed and process-tree termination on timeout (exit code `124`), cleanly terminating hung background threads or child processes on **macOS, Linux, and Windows (PowerShell / CMD / Git Bash)**.
- **Large-Suite Acceleration**: Supports `--target SUBPATH`, `-k EXPR` filtering, `--python PATH` cross-environment execution, and `--isolated-scope smart` (running expensive single-test process isolation only for tests that failed or flipped in suite runs).

### Stage 2 — Statistical Scoring & Polluter Inference (`src/analysis/`)
- **Crash-Resilient Stability Scoring**: Computes `passed / (passed + failed + error)` across valid observations while preventing incomplete, timed-out, or crashed runs from producing false condition attributions.
- **Bidirectional Isolation Attribution**: Detects both **polluted victims** (fail in suite runs, pass when isolated) and **state consumers** (pass in suite runs because an earlier test initialized shared state, but fail when run in isolation).
- **Automated Polluter Identification (`suspected_polluters`)**: Tracks exact execution positions in every suite run and computes `common_on_fail - ever_on_pass` across predecessor sets (with symmetric mutual-polluter refinement) to pinpoint which earlier test dirtied the shared state.
- **Small-Sample False-Positive Guards**: Protects low-probability probabilistic tests that happen not to flip during the finite `repeat` baseline from being misclassified as deterministic hash-seed or order bugs.

### Stage 3 — Static Python AST Disambiguation & Triage Report (`src/report/`)
- **Runtime + AST Disambiguation & Refinement**: When runtime behaviour is ambiguous (both `unseeded_randomness` and `race_conditions` vary under identical seed and test order), inspects Python Abstract Syntax Trees (AST) to narrow the root cause (`AST Hint *`); when runtime evidence points to `hash_order_sensitivity` or `shared_state_interference` and AST identifies strict floating-point equality (`float_precision`) or unisolated file/port resources (`resource_leak`), refines the category (`AST Refined *`) while preserving the original runtime candidates and assigning fault ownership (`Test Side` vs. `Code Side`).
- **Deep Cross-File Call Graph & Fixture Resolution**:
  - Follows imported functions, methods, and class instantiations up to **3 hops** across project modules (including relative imports).
  - Resolves `@pytest.fixture` definitions across the test file and ancestor `conftest.py` files.
  - Tracks test class `setup_method` / `setUp` initializations and `self.<attr>` references.
  - Detects both `Test Side` shared module/fixture state and `Code Side` production module global/class-attribute mutations.
- **Broad Pattern Coverage with False-Positive Suppression**:
  - Detects `random.*`, `uuid.uuid4`, `secrets.*`, `os.urandom`, `numpy.random`, `threading.Thread`, `ThreadPoolExecutor`, `asyncio.gather`, naive local-time calls (`datetime.now()`, `date.today()`, `datetime.fromtimestamp(ts)` without `tz`, `time.localtime()`, `time.strftime()` without `gmtime`), hardcoded file/database/port resource leaks (`open(..., "w")`, `Path(...).write_text()`, `sqlite3.connect("...")`, `.bind((host, 8080))` with `port != 0` -> `resource_leak`), unmocked external network calls (`requests.*`, `httpx.*`, `urllib.request.urlopen`, `aiohttp.ClientSession`, `socket.create_connection` -> `external_io`), and strict floating-point equality assertions (`assert a == b` on floats/`sum(...)`/division -> `float_precision`).
  - Automatically suppresses false positives when deterministic seeds (`random.seed(42)`), sorted collections (`sorted(...)`), path joins (`os.path.join`), explicit state reset helpers (`reset_*` / `clear_*`), explicit timezone arguments (`tz=timezone.utc`, `time.gmtime`), time-freeze tools (`@freeze_time`, `freezegun`, `freezer` fixture, `monkeypatch.setenv("TZ", ...)`), isolated temporary paths (`tmp_path`, `tmpdir`, `tempfile.*`, `:memory:`, ephemeral port `0`), network mocks (`monkeypatch`, `mocker`, `@patch`, `responses`, `respx`, `httpretty`, `requests_mock`), or approximate float comparisons (`pytest.approx`, `math.isclose`, `np.isclose`, `round`, `Decimal`) are present.
- **Actionable Outputs**: Generates both a machine-readable `diagnosis.json` (with persisted AST resolution fields for CI pipelines) and a human-readable `report.md` featuring failure-prioritized log links and `file:line` source hints.

---

## Installation & Quick Start

Requires **Python 3.10+** and supports **macOS**, **Linux**, and **Windows (PowerShell / CMD / Git Bash)**.

### Mode 1: Install Only the CLI Tool (`pip install`, without `scenario/`)

If you want to install `pytest-flaky-triage` as a lightweight command-line tool to diagnose your own projects, `pip install` packages **only** the core triage engine (`flaky_triage`, `analysis`, `report`) and **does not** install the demo `scenario/` or test fixtures into your environment:

**macOS / Linux:**
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install git+https://github.com/Raintrrrreeeee/pytest-flaky-triage.git

# Run directly on any pytest project:
pytest-flaky-triage --project /path/to/your/project --out ./triage-out
```

**Windows (PowerShell):**
```powershell
python -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
pip install git+https://github.com/Raintrrrreeeee/pytest-flaky-triage.git

# Run directly on any pytest project:
pytest-flaky-triage --project C:\path\to\your\project --out .\triage-out
```

#### Optional: Download Only the `scenario/` Benchmark Playground On Demand

If you installed the CLI via `pip install` above and want to try it on the built-in 22-test e-commerce benchmark suite (`scenario/`) first, you can download just the `scenario/` folder without cloning the full repository history:

```bash
git clone --depth 1 --filter=blob:none --sparse https://github.com/Raintrrrreeeee/pytest-flaky-triage.git flaky-demo
cd flaky-demo
git sparse-checkout set --no-cone /scenario /docs

# Run the installed CLI against the downloaded scenario (or simply run `pytest-flaky-triage --demo`):
pytest-flaky-triage --project ./scenario --out ./out
cat ./out/report.md

# Optional: switch report.md to Simplified Chinese instantly without re-running pytest:
pytest-flaky-triage --project ./scenario --out ./out --lang zh --report-only
```

---

### Mode 2: Full Repository Clone (Development, Demo & Self-Tests)

If you want the full source tree, the `scenario/` benchmark suite, and the tool's 132-test verification suite in one place:

**macOS / Linux:**
```bash
git clone https://github.com/Raintrrrreeeee/pytest-flaky-triage.git
cd pytest-flaky-triage
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .

# 1. Run the 22-test benchmark scenario end to end (~12s):
pytest-flaky-triage --demo

# 2. Inspect the generated Markdown triage report:
cat out/report.md

# 3. Run the tool's own 132 unit and integration tests:
bash scripts/test.sh
```

**Windows (PowerShell):**
```powershell
git clone https://github.com/Raintrrrreeeee/pytest-flaky-triage.git
cd pytest-flaky-triage
python -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -e .

# 1. Run the 22-test benchmark scenario end to end (~12s):
pytest-flaky-triage --demo

# 2. Inspect the generated Markdown triage report:
Get-Content -Encoding utf8 out\report.md

# 3. Run the tool's own 132 unit and integration tests:
pytest tests -p no:randomly -p no:cacheprovider --ignore=tests/fixtures
```

---

## CLI Usage & Perturbation Matrix

```bash
pytest-flaky-triage --project PATH [options]
# or from a repository checkout:
scripts/detect.sh --project PATH [options]
```

Each condition changes one factor and keeps the others fixed:

| Condition | What changes | Fixed | Exposes |
|---|---|---|---|
| `repeat` | nothing: the same command again | test order, `PYTHONHASHSEED=0`, `TZ=UTC` | unseeded randomness, race conditions |
| `hashseed` | `PYTHONHASHSEED`, one run per seed | test order, `TZ=UTC` | set / hash-order dependence |
| `order` | test order: reversed, then seeded shuffles (`O(N)` hook reorder) | `PYTHONHASHSEED=0`, `TZ=UTC` | order dependence, shared state |
| `tz` | `TZ` environment variable (`UTC`, `Pacific/Kiritimati`, `America/Pago_Pago`) | test order, `PYTHONHASHSEED=0` | naive local-time / UTC date-boundary sensitivity |
| `isolated` | each test runs alone in its own process | `PYTHONHASHSEED=0`, `TZ=UTC` | shared state (victim or state-consumer) |

### Command-Line Options

| Option | Default | Meaning |
|---|---|---|
| `--project PATH` | (required) | pytest project to investigate |
| `--target SUBPATH` | (entire project) | restrict collection to a specific test file, directory, or node ID |
| `-k EXPR` | (none) | only collect tests matching pytest keyword expression `EXPR` |
| `--repeats N` | `10` | plain repeated runs (`N >= 1`) |
| `--hash-seeds "S..."` | `"1 2 3 4 5 6 7 8"` | `PYTHONHASHSEED` values, one run each; `""` to skip |
| `--orders K` | `5` | seeded shuffled-order runs plus one reversed run; `0` to skip |
| `--timezones "TZ..."` | `"UTC Pacific/Kiritimati America/Pago_Pago"` | `TZ` timezone values, one run each; `""` to skip |
| `--isolated N` | `1` | runs of each test on its own; `0` to skip |
| `--isolated-scope MODE` | `all` | `all` runs every collected test in isolation; `smart` runs only tests that failed or flipped in earlier conditions |
| `--timeout SECONDS` | `60` | hard time limit for a single pytest run |
| `--out DIR` | `out/` | output folder (rejects `/`, `$HOME`, symlinks, and folders containing `--project`) |
| `--python PATH` | auto-detected | interpreter with `pytest` and the target project's dependencies installed |
| `--lang LANG` | `en` | report language for `report.md`: `en` (English) or `zh` (Simplified Chinese) |
| `--report-only` | off | regenerate `report.md` from existing `out/` results without re-running `pytest` |
| `--no-report` | off | stop after writing `results.csv` and `run_meta.csv` |

### Exit Codes

| Code | Meaning |
|---|---|
| `0` | finished; `results.csv`, `run_meta.csv`, `diagnosis.json`, and `report.md` written |
| `2` | invalid command-line arguments |
| `3` | Python or `pytest` not found |
| `4` | `pytest` could not collect the project, or found no tests |
| `5` | no run produced readable results |
| `6` | the analysis or report stage failed |

### Output Files (`out/`)

| File | Contents |
|---|---|
| `results.csv` | one row per test per run: `run_id, condition, condition_value, position, test_id, outcome` |
| `run_meta.csv` | one row per run: `run_id, condition, condition_value, hash_seed, exit_code, status, seconds, tests_expected, tests_reported, log_path` |
| `logs/run_NNN.log` | raw pytest output of each run |
| `orders/` | the test order used by each `order` run |
| `collected.txt`, `collect.log` | the collected test IDs and pytest's collection output |
| `diagnosis.json` | structured stability scores, verdicts, runtime suspicions, `suspected_polluters`, and AST-disambiguated `resolution` |
| `report.md` | human-readable Markdown triage report with AST source heuristic hints and failure-prioritized log links |

---

## Repository Structure

```text
scripts/
  detect.sh              Stage 1 condition-matrix runner (Bash)
  lib/flaky_plugin.py    pytest sideband hook plugin, O(N) reorder, and process-group timeout controller
  lib/parse_log.awk      hardened AWK log-parser fallback
  demo.sh                runs the demonstration scenario end to end
  test.sh                runs the tool's own 132-test verification suite
src/
  analysis/              Stage 2 stability scoring, root-cause inference, and polluter identification (`python -m analysis`)
  report/                Stage 3 AST source heuristics and Markdown triage report generator (`python -m report`)
  flaky_triage/          pip-installable CLI package (`pytest-flaky-triage`) with bundled runtime scripts
tests/
  test_parser.py         parser fixture tests: each fixture's results.csv vs its expected.csv
  test_runner.py         argument checks, exit codes, output-folder safety
  test_analysis.py       stability scoring, verdicts, incomplete-run handling, polluter identification, and CLI contract
  test_report.py         schema validation, AST heuristics, Markdown rendering, and CLI contract
  test_demo.py           end-to-end demo.sh verification against the answer key
  fixtures/              parser and analysis test fixtures
scenario/                optional 22-test controlled flaky-test benchmark suite (excluded from wheel/pip installs)
docs/
  contracts.md           Stage 1 / Stage 2 / Stage 3 data contracts and 8-category taxonomy
  scenario.md            benchmark answer key: every planted case and expected classification
pyproject.toml           PEP 621 package and CLI entry point configuration
requirements.txt         runtime dependencies
LICENSE                  MIT License
```

---

## Controlled Benchmark Scenario (`scenario/`)

The repository includes an optional simulated **E-commerce Cart and Checkout System** (`scenario/src/ecommerce/` and `scenario/tests/test_cart.py`) exhibiting five root-cause categories of flaky tests plus deterministic controls (22 tests in total; full details in [docs/scenario.md](docs/scenario.md)):

1. **Hash Order Sensitivity (`hash_order_sensitivity`, Test Side, 4 tests):** Tests join unordered Python `set` objects into strings (`",".join(...)`) and assert a hardcoded element order, flipping across `PYTHONHASHSEED` values.
2. **Unseeded Randomness (`unseeded_randomness`, Code Side, 4 tests):** Production methods call unseeded `random.choice(...)` while tests assert a single fixed return value.
3. **Race Conditions (`race_conditions`, Code Side, 4 tests):** `Cart.checkout()` spawns an unjoined background `threading.Thread` to deduct inventory, racing against the test's immediate stock assertion.
4. **Shared State Interference (`shared_state_interference`, Test Side, 4 tests):** Two pairs of tests mutate a shared module-level `Cart` instance (`shared_test_cart`) without cleanup, failing depending on execution order and passing in isolation.
5. **Timezone / Local-Time Sensitivity (`timezone_sensitivity`, 2 Code Side & 2 Test Side, 4 tests):** Production methods and tests mix naive local time (`datetime.now()`, `date.today()`, `datetime.fromtimestamp(ts)` without `tz`, `time.localtime(ts)`) with UTC expectations, flipping when `TZ` is perturbed across extreme UTC offsets (`UTC`, `Pacific/Kiritimati`, `America/Pago_Pago`).
6. **Deterministic Controls (`stable`, 2 tests):** Purely local, deterministic, synchronous tests that pass 100% of the time across all conditions.

## Acknowledgements

Special thanks to **Hongguan Tan** and **Huizern Lim** for their valuable collaboration, discussions, and contributions to the initial prototype and controlled benchmark scenario design that inspired this standalone v2 tool.

## License

Released under the [MIT License](LICENSE).
