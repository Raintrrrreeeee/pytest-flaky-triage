# pytest-flaky-triage

A three-stage flaky-test detection, statistical root-cause inference, and AST fault-side triage tool for Python `pytest` suites.

It runs a target `pytest` suite across controlled perturbation conditions (`repeat`, `hashseed`, `order`, and `isolated`) using a real-time pytest hook sideband plugin (with an AWK log-parser fallback), computes per-test stability scores and root-cause correlations, identifies polluting predecessor tests from execution-order positions, and combines runtime evidence with static Python AST inspection to pinpoint whether the defect lives in the test (`Test Side`) or in production code (`Code Side`).

## Quick Start

Requires Bash and Python 3.10+. Run from the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .                 # optional: installs `pytest-flaky-triage` CLI
scripts/demo.sh                  # demonstration scenario, end to end (~15 s)
scripts/test.sh                  # the tool's own test suite
```

Nothing needs network access after `pip install`, and default output goes to `out/` inside the repository.

## Usage

```bash
scripts/detect.sh --project PATH [options]
# or, after `pip install -e .`:
pytest-flaky-triage --project PATH [options]
```

Runs the pytest project in `PATH` under four controlled perturbation conditions. Each changes one factor and keeps the others fixed:

| Condition | What changes | Fixed | Exposes |
|---|---|---|---|
| `repeat` | nothing: the same command again | test order, `PYTHONHASHSEED=0` | unseeded randomness, race conditions |
| `hashseed` | `PYTHONHASHSEED`, one run per seed | test order | set / hash-order dependence |
| `order` | test order: reversed, then seeded shuffles (`O(N)` hook reorder) | `PYTHONHASHSEED=0` | order dependence, shared state |
| `isolated` | each test runs alone in its own process | `PYTHONHASHSEED=0` | shared state (victim or state-consumer) |

Every run executes in an isolated process group with `stdin` closed (`/dev/null`), neutralizes conflicting third-party pytest plugins (`pytest-randomly`, `pytest-sugar`, `pytest-xdist`, `pytest-cov`, `pytest-rerunfailures`, `pytest-order`, etc.), and enforces a hard per-run timeout with `SIGTERM -> SIGKILL` escalation.

| Option | Default | Meaning |
|---|---|---|
| `--project PATH` | (required) | pytest project to investigate |
| `--target SUBPATH` | (entire project) | restrict collection to a specific test file, directory, or node ID |
| `-k EXPR` | (none) | only collect tests matching pytest keyword expression `EXPR` |
| `--repeats N` | 10 | plain repeated runs (`N >= 1`) |
| `--hash-seeds "S..."` | `"1 2 3 4 5 6 7 8"` | `PYTHONHASHSEED` values, one run each; `""` to skip |
| `--orders K` | 5 | seeded shuffled-order runs plus one reversed run; `0` to skip |
| `--isolated N` | 1 | runs of each test on its own; `0` to skip |
| `--isolated-scope MODE` | `all` | `all` runs every collected test in isolation; `smart` runs only tests that failed or flipped in earlier conditions |
| `--timeout SECONDS` | 60 | limit for one pytest run |
| `--out DIR` | `out/` | output folder (supports internal or external paths; rejects `/`, `$HOME`, symlinks, and folders containing `--project`) |
| `--python PATH` | `.venv/bin/python` | interpreter with `pytest` and the target project's dependencies installed |
| `--no-report` | | stop after writing `results.csv` and `run_meta.csv` |

`scripts/demo.sh` passes any options on to `detect.sh`, e.g. `scripts/demo.sh --repeats 20`.
`scripts/test.sh` passes options on to `pytest`, e.g. `scripts/test.sh -k parser`.

### Exit codes of `detect.sh`

| Code | Meaning |
|---|---|
| 0 | finished; `results.csv`, `run_meta.csv`, `diagnosis.json`, and `report.md` written |
| 2 | bad arguments |
| 3 | Python or pytest not found |
| 4 | pytest could not collect the project, or found no tests |
| 5 | no run produced readable results |
| 6 | the analysis or report stage failed |

### Output files (`out/`)

| File | Contents |
|---|---|
| `results.csv` | one row per test per run: `run_id, condition, condition_value, position, test_id, outcome` |
| `run_meta.csv` | one row per run: `run_id, condition, condition_value, hash_seed, exit_code, status, seconds, tests_expected, tests_reported, log_path` |
| `logs/run_NNN.log` | raw pytest output of each run |
| `orders/` | the test order used by each `order` run |
| `collected.txt`, `collect.log` | the collected test IDs and pytest's collection output |
| `diagnosis.json` | structured stability scores, verdicts, runtime suspicions, suspected polluters, and AST-disambiguated resolutions |
| `report.md` | human-readable Markdown triage report with AST source-based heuristic hints and failure-prioritized log links |

`outcome` is one of `passed, failed, error, skipped, xfailed, xpassed, timeout, not_run`.
`timeout` marks the test that was running when a run hit its time limit; tests that never got to run are `not_run`.

## Repository Structure

```text
scripts/
  detect.sh              Stage 1 condition-matrix runner (Bash)
  lib/flaky_plugin.py    pytest sideband hook plugin, O(N) reorder, and process-group timeout controller
  lib/parse_log.awk      hardened AWK log-parser fallback
  demo.sh                runs the demonstration scenario end to end
  test.sh                runs the tool's own test suite
src/
  analysis/              Stage 2 stability scoring, root-cause inference, and polluter identification (`python -m analysis`)
  report/                Stage 3 AST source heuristics and Markdown triage report generator (`python -m report`)
  flaky_triage/          CLI entry point (`pytest-flaky-triage`)
tests/
  test_parser.py         parser fixture tests: each fixture's results.csv vs its expected.csv
  test_runner.py         argument checks, exit codes, output-folder safety
  test_analysis.py       stability scoring, verdicts, incomplete-run handling, polluter identification, and CLI contract
  test_report.py         schema validation, AST heuristics, Markdown rendering, and CLI contract
  test_demo.py           end-to-end demo.sh verification against the answer key
  fixtures/              parser and analysis test fixtures
scenario/                the 18-test controlled flaky-test benchmark suite (src/ and tests/)
docs/
  scenario.md            benchmark answer key: every planted case and expected classification
pyproject.toml           Python package and CLI entry point configuration
requirements.txt         runtime dependencies
```

## Controlled Benchmark Scenario (`scenario/`)

This repository includes a simulated **E-commerce Cart and Checkout System** (`scenario/src/ecommerce/` and `scenario/tests/test_cart.py`) exhibiting four root-cause categories of flaky tests plus deterministic controls (18 tests in total; full details in [docs/scenario.md](docs/scenario.md)):

1. **Hash Order Sensitivity (`hash_order_sensitivity`, Test Side, 4 tests):** Tests join unordered Python `set` objects into strings (`",".join(...)`) and assert a hardcoded element order, flipping across `PYTHONHASHSEED` values.
2. **Unseeded Randomness (`unseeded_randomness`, Code Side, 4 tests):** Production methods call unseeded `random.choice(...)` while tests assert a single fixed return value.
3. **Race Conditions (`race_conditions`, Code Side, 4 tests):** `Cart.checkout()` spawns an unjoined background `threading.Thread` to deduct inventory, racing against the test's immediate stock assertion.
4. **Shared State Interference (`shared_state_interference`, Test Side, 4 tests):** Two pairs of tests mutate shared module-level `Cart` instances (`shared_test_cart` and `secondary_shared_cart`) without cleanup, failing depending on execution order and passing in isolation.
5. **Deterministic Controls (`stable`, 2 tests):** Purely local, deterministic, synchronous tests that pass 100% of the time across all conditions.
