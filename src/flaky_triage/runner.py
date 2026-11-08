"""Cross-platform pure-Python Stage 1 matrix runner and demo orchestrator.

Used natively on Windows (PowerShell / CMD / Conda / Git Bash) and on any
platform where Bash is unavailable or `FLAKY_PURE_PYTHON=1` is set.
Implements the exact CLI contract, exit codes, safety checks, and output files
of `scripts/detect.sh` and `scripts/demo.sh`.
"""

import csv
import io
import json
import os
from pathlib import Path
import random
import re
import shutil
import subprocess
import sys
import time

E_USAGE = 2      # bad arguments
E_ENV = 3        # Python or pytest missing
E_COLLECT = 4    # pytest could not collect the project, or found no tests
E_RUNS = 5       # no run produced parseable results
E_REPORT = 6     # analysis / report stage failed

_RANK = {
    "error": 3,
    "failed": 2,
    "passed": 1,
    "skipped": 1,
    "xfailed": 1,
    "xpassed": 1,
}

_SUMMARY_SEC_RE = re.compile(r"^=+ .* in ([0-9]+\.[0-9]+)s.*=+$")
_VERBOSE_OUTCOME_RE = re.compile(
    r"^(\S+::.*?) (PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)(?:\s+\[.*\])?\s*$"
)


def _is_uint(val):
    return bool(re.match(r"^[0-9]{1,9}$", str(val)))


def _csv_quote(text):
    escaped = str(text).replace('"', '""')
    return f'"{escaped}"'


def _locate_paths():
    pkg_dir = Path(__file__).resolve().parent
    repo_root = pkg_dir.parents[1]
    if (repo_root / "scripts" / "lib" / "flaky_plugin.py").is_file():
        lib_dir = repo_root / "scripts" / "lib"
        tool_root = repo_root
        installed = os.environ.get("FLAKY_INSTALLED_PACKAGE") == "1"
    else:
        lib_dir = pkg_dir / "runtime" / "lib"
        tool_root = pkg_dir
        installed = True
    return pkg_dir, repo_root, tool_root, lib_dir, installed


def _usage_text(cli_name):
    return f"""Usage: {cli_name} --project PATH [options]

Runs the pytest suite in PATH under several conditions and writes
per-test outcomes to OUT/results.csv.

Options:
  --project PATH             pytest project to investigate (required)
  --target SUBPATH           optional test file or directory inside project
  -k EXPR                    only run tests matching pytest keyword expression
  --repeats N                plain repeated runs, N >= 1 (default 10)
  --hash-seeds "S..."        PYTHONHASHSEED values, one run each; "" to skip
                             (default "1 2 3 4 5 6 7 8")
  --orders K                 seeded shuffled-order runs, plus one reversed run;
                             0 to skip (default 5)
  --isolated N               runs of each test on its own; 0 to skip (default 1)
  --isolated-scope MODE      'all' (every test) or 'smart' (only tests with
                             suite failures) (default all)
  --timeout SECONDS          limit for a single pytest run (default 60)
  --out DIR                  output folder (default out/)
  --python PATH              Python interpreter with pytest installed for target
  --lang LANG                report language: 'en' or 'zh' (default en)
  --report-only              regenerate report.md from existing output without
                             re-running pytest
  --no-report                stop after writing results.csv
  -h, --help                 show this help
"""


def _events_or_log_to_rows(run_id, condition, cvalue, timed_out, expected_ids, events_file, log_file):
    expected_set = set(expected_ids)
    outcomes = {}
    positions = {}
    started = set()
    finished = set()
    n_reported = 0
    any_event = False

    if events_file.is_file():
        try:
            for raw_line in events_file.read_text(encoding="utf-8", errors="replace").splitlines():
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    ev = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                any_event = True
                etype = ev.get("event")
                nid = ev.get("nodeid")
                if nid not in expected_set:
                    continue
                if etype == "start":
                    started.add(nid)
                elif etype == "report":
                    out = ev.get("outcome")
                    if out not in _RANK:
                        continue
                    if nid not in outcomes:
                        outcomes[nid] = out
                        n_reported += 1
                        positions[nid] = n_reported
                    elif _RANK[out] > _RANK[outcomes[nid]]:
                        outcomes[nid] = out
                elif etype == "finish":
                    finished.add(nid)
        except OSError:
            pass

    if not any_event and log_file.is_file():
        map_out = {
            "PASSED": "passed",
            "FAILED": "failed",
            "ERROR": "error",
            "SKIPPED": "skipped",
            "XFAIL": "xfailed",
            "XPASS": "xpassed",
        }
        try:
            for raw_line in log_file.read_text(encoding="utf-8", errors="replace").splitlines():
                m = _VERBOSE_OUTCOME_RE.match(raw_line.strip())
                if not m:
                    continue
                nid, raw_tok = m.group(1), m.group(2)
                if nid not in expected_set:
                    continue
                out = map_out[raw_tok]
                if nid not in outcomes:
                    outcomes[nid] = out
                    n_reported += 1
                    positions[nid] = n_reported
                elif _RANK[out] > _RANK[outcomes[nid]]:
                    outcomes[nid] = out
        except OSError:
            pass

    teardown_timeout_id = None
    if timed_out and any_event:
        for nid in expected_ids:
            if nid in started and nid not in finished and nid in outcomes:
                teardown_timeout_id = nid
                break

    rows = []
    n_parsed = 0
    missing_seen = False
    for nid in expected_ids:
        if nid == teardown_timeout_id:
            out = "timeout"
            pos = ""
            missing_seen = True
        elif nid in outcomes:
            out = outcomes[nid]
            pos = str(positions[nid])
            n_parsed += 1
        else:
            out = "timeout" if (timed_out and not missing_seen) else "not_run"
            missing_seen = True
            pos = ""
        rows.append(f"{run_id},{condition},{_csv_quote(cvalue)},{pos},{_csv_quote(nid)},{out}")
    return rows, n_parsed


def _run_subprocess_with_timeout(cmd, cwd, env, timeout_secs, log_file):
    is_win = os.name == "nt"
    with log_file.open("w", encoding="utf-8", errors="replace") as log_handle:
        popen_kwargs = {
            "cwd": str(cwd),
            "env": env,
            "stdin": subprocess.DEVNULL,
            "stdout": log_handle,
            "stderr": subprocess.STDOUT,
        }
        if is_win:
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            popen_kwargs["start_new_session"] = True
        proc = subprocess.Popen(cmd, **popen_kwargs)
        try:
            rc = proc.wait(timeout=timeout_secs)
            return (128 + (-rc)) if rc < 0 else rc
        except subprocess.TimeoutExpired:
            if is_win:
                try:
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                    )
                except OSError:
                    pass
                try:
                    proc.kill()
                except OSError:
                    pass
            else:
                import signal

                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                    try:
                        proc.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                pass
            return 124


def _find_target_python(explicit_python, project_dir, tool_python):
    if explicit_python:
        return explicit_python
    candidates = [
        project_dir / ".venv" / "Scripts" / "python.exe",
        project_dir / ".venv" / "bin" / "python",
        project_dir / "venv" / "Scripts" / "python.exe",
        project_dir / "venv" / "bin" / "python",
    ]
    venv_env = os.environ.get("VIRTUAL_ENV")
    if venv_env:
        candidates.append(Path(venv_env) / "Scripts" / "python.exe")
        candidates.append(Path(venv_env) / "bin" / "python")
    candidates.append(Path(tool_python))

    for cand in candidates:
        if cand.is_file():
            try:
                res = subprocess.run(
                    [str(cand), "-m", "pytest", "--version"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=15,
                    check=False,
                )
                if res.returncode == 0:
                    return str(cand)
            except OSError:
                continue
    return str(tool_python)


def _check_pytest_available(python_bin):
    p = Path(python_bin)
    if not (p.is_file() or shutil.which(python_bin)):
        return False, ""
    try:
        res = subprocess.run(
            [python_bin, "-m", "pytest", "--version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if res.returncode == 0:
            first_line = (res.stdout or res.stderr).splitlines()[0] if (res.stdout or res.stderr) else "pytest"
            return True, first_line
    except OSError:
        pass
    return False, ""


def run_detect(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    _, repo_root, tool_root, lib_dir, installed = _locate_paths()
    cli_usage = os.environ.get("FLAKY_CLI_NAME", "pytest-flaky-triage")
    cli_err = os.environ.get("FLAKY_CLI_NAME", "detect.sh")
    is_tty = sys.stderr.isatty()

    def say(msg):
        if is_tty:
            sys.stderr.write("\r\033[K")
        sys.stderr.write(f"[detect] {msg}\n")
        sys.stderr.flush()

    def die(msg, code=E_USAGE):
        if is_tty:
            sys.stderr.write("\r\033[K")
        sys.stderr.write(f"{cli_err}: error: {msg}\n")
        sys.stderr.flush()
        return code

    project_raw = ""
    target_subpath = ""
    keyword_expr = ""
    repeats_raw = "10"
    hash_seeds_raw = "1 2 3 4 5 6 7 8"
    orders_raw = "5"
    isolated_raw = "1"
    isolated_scope = "all"
    timeout_raw = "60"
    out_raw = str((Path.cwd() / "out") if installed else (tool_root / "out"))
    python_raw = ""
    report_lang = "en"
    do_report = True
    report_only = False

    value_opts = {
        "--project", "--target", "-k", "--repeats", "--hash-seeds",
        "--orders", "--isolated", "--isolated-scope", "--timeout",
        "--out", "--python", "--lang",
    }

    idx = 0
    while idx < len(args):
        tok = args[idx]
        if tok in ("-h", "--help"):
            sys.stdout.write(_usage_text(cli_usage))
            return 0
        if tok == "--no-report":
            do_report = False
            idx += 1
            continue
        if tok == "--report-only":
            report_only = True
            idx += 1
            continue
        if tok.startswith("--") and "=" in tok:
            opt_name, opt_val = tok.split("=", 1)
            args[idx : idx + 1] = [opt_name, opt_val]
            continue
        if tok not in value_opts:
            return die(f"unknown option '{tok}' (see --help)")
        if idx + 1 >= len(args):
            return die(f"{tok} needs a value (see --help)")
        val = args[idx + 1]
        if tok == "--project":
            project_raw = val
        elif tok == "--target":
            target_subpath = val
        elif tok == "-k":
            keyword_expr = val
        elif tok == "--repeats":
            repeats_raw = val
        elif tok == "--hash-seeds":
            hash_seeds_raw = val
        elif tok == "--orders":
            orders_raw = val
        elif tok == "--isolated":
            isolated_raw = val
        elif tok == "--isolated-scope":
            isolated_scope = val
        elif tok == "--timeout":
            timeout_raw = val
        elif tok == "--out":
            out_raw = val
        elif tok == "--python":
            python_raw = val
        elif tok == "--lang":
            report_lang = val
        idx += 2

    if not project_raw:
        sys.stderr.write(_usage_text(cli_usage))
        return die("--project is required")
    project_path = Path(project_raw)
    if not project_path.is_dir():
        return die(f"project folder '{project_raw}' does not exist")
    project_dir = project_path.resolve()

    if not _is_uint(repeats_raw) or int(repeats_raw, 10) < 1:
        return die(f"--repeats must be a whole number >= 1 (got '{repeats_raw}')")
    if not _is_uint(orders_raw):
        return die(f"--orders must be a whole number >= 0 (got '{orders_raw}')")
    if not _is_uint(isolated_raw):
        return die(f"--isolated must be a whole number >= 0 (got '{isolated_raw}')")
    if not _is_uint(timeout_raw) or int(timeout_raw, 10) < 1:
        return die(f"--timeout must be a whole number of seconds >= 1 (got '{timeout_raw}')")
    if isolated_scope not in ("all", "smart"):
        return die(f"--isolated-scope must be 'all' or 'smart' (got '{isolated_scope}')")
    if report_lang not in ("en", "zh"):
        return die(f"--lang must be 'en' or 'zh' (got '{report_lang}')")
    if not do_report and report_only:
        return die("--no-report and --report-only cannot be used together")

    repeats = int(repeats_raw, 10)
    orders = int(orders_raw, 10)
    isolated = int(isolated_raw, 10)
    timeout_secs = int(timeout_raw, 10)

    seed_arr = []
    for s_tok in hash_seeds_raw.split():
        if not _is_uint(s_tok):
            return die(f"--hash-seeds values must be whole numbers 0..999999999 (got '{s_tok}')")
        seed_arr.append(int(s_tok, 10))

    out_candidate = Path(out_raw)
    if out_candidate.is_symlink():
        return die(f"--out must not be a symlink (got '{out_raw}')")
    try:
        out_candidate.mkdir(parents=True, exist_ok=True)
    except OSError:
        return die(f"cannot create output folder '{out_raw}'")
    out_dir = out_candidate.resolve()

    home_dir = Path.home().resolve()
    if out_dir == Path(out_dir.anchor) or str(out_dir) == "/" or out_dir == home_dir:
        return die(f"--out must not be / or HOME (got '{out_dir}')")
    if not installed and out_dir == tool_root.resolve():
        return die(f"--out must be a sub-folder or dedicated directory, not {tool_root}")
    if out_dir == project_dir:
        return die("--out must not be the project folder")
    try:
        project_dir.relative_to(out_dir)
        return die("--out must not contain the project folder")
    except ValueError:
        pass
    if not installed:
        try:
            tool_root.resolve().relative_to(out_dir)
            return die("--out must not contain the tool folder")
        except ValueError:
            pass

    try:
        out_rel = out_dir.relative_to(tool_root.resolve()).as_posix() if not installed else str(out_dir)
    except ValueError:
        out_rel = str(out_dir)

    tool_python = os.environ.get("FLAKY_TOOL_PYTHON") or sys.executable
    tool_pythonpath = os.pathsep.join(
        [p for p in [str(repo_root / "src") if (repo_root / "src").is_dir() else "", os.environ.get("PYTHONPATH", "")] if p]
    )

    def run_analysis_and_report():
        diag_path = out_dir / "diagnosis.json"
        res_csv = out_dir / "results.csv"
        if report_only and not diag_path.is_file() and not res_csv.is_file():
            return die(
                f"--report-only requires an existing {out_rel}/diagnosis.json or {out_rel}/results.csv",
                E_REPORT,
            )
        env = os.environ.copy()
        if tool_pythonpath:
            env["PYTHONPATH"] = tool_pythonpath
        if not report_only or not diag_path.is_file():
            proc_a = subprocess.run(
                [tool_python, "-m", "analysis", str(res_csv), str(out_dir)],
                env=env,
                check=False,
            )
            a_rc = proc_a.returncode
            if a_rc == 0:
                say(f"diagnosis: {out_rel}/diagnosis.json")
            elif a_rc == 2:
                return die("analysis stage rejected its arguments (exit 2)", E_REPORT)
            elif a_rc == 3:
                return die("analysis stage could not read results.csv or run_meta.csv (exit 3)", E_REPORT)
            elif a_rc == 4:
                return die("analysis stage found invalid or inconsistent data in results.csv (exit 4)", E_REPORT)
            elif a_rc == 5:
                return die("analysis stage could not write diagnosis.json (exit 5)", E_REPORT)
            else:
                return die(f"analysis stage failed with exit code {a_rc}", E_REPORT)

        proc_r = subprocess.run(
            [
                tool_python,
                "-m",
                "report",
                str(diag_path),
                str(out_dir),
                "--project",
                str(project_dir),
                "--lang",
                report_lang,
            ],
            env=env,
            check=False,
        )
        r_rc = proc_r.returncode
        if r_rc == 0:
            say(f"report:    {out_rel}/report.md")
            return 0
        if r_rc == 2:
            return die("report stage rejected its arguments (exit 2)", E_REPORT)
        if r_rc == 3:
            return die("report stage could not read diagnosis.json (exit 3)", E_REPORT)
        if r_rc == 4:
            return die("report stage found invalid or inconsistent data in diagnosis.json (exit 4)", E_REPORT)
        if r_rc == 5:
            return die("report stage could not write report.md (exit 5)", E_REPORT)
        return die(f"report stage failed with exit code {r_rc}", E_REPORT)

    if report_only:
        return run_analysis_and_report()

    target_python = _find_target_python(python_raw, project_dir, tool_python)
    ok_pytest, pytest_ver_line = _check_pytest_available(target_python)
    if not ok_pytest:
        return die(
            f"pytest is not installed for {target_python}; run: pip install -r requirements.txt",
            E_ENV,
        )

    py_path_parts = [str(lib_dir)]
    if (project_dir / "src").is_dir():
        py_path_parts.append(str(project_dir / "src"))
    py_path_parts.append(str(project_dir))
    if os.environ.get("PYTHONPATH"):
        py_path_parts.append(os.environ["PYTHONPATH"])
    target_pythonpath = os.pathsep.join(py_path_parts)

    pytest_opts = [
        f"--rootdir={project_dir}",
        "-o", "console_output_style=progress",
        "-p", "flaky_plugin",
        "-p", "no:randomly",
        "-p", "no:random_order",
        "-p", "no:order",
        "-p", "no:ordering",
        "-p", "no:reverse",
        "-p", "no:cacheprovider",
        "-p", "no:sugar",
        "-p", "no:xdist",
        "-p", "no:cov",
        "-p", "no:flaky",
        "-p", "no:rerunfailures",
        "-p", "no:flakefinder",
        "-p", "no:instafail",
        "--maxfail=0",
        "--tb=short",
        "--verbosity=1",
        "--color=no",
        "--capture=fd",
    ]
    try:
        out_dir.relative_to(project_dir)
        pytest_opts.append(f"--ignore={out_dir}")
    except ValueError:
        pass

    extra_collect_args = []
    if keyword_expr:
        extra_collect_args.extend(["-k", keyword_expr])
    if target_subpath:
        extra_collect_args.append(target_subpath)

    say(f"project: {project_dir}")
    say(f"python:  {target_python} ({pytest_ver_line})")

    collect_log = out_dir / "collect.log"
    collect_tmp = out_dir / "collected.tmp"
    collect_tmp.unlink(missing_ok=True)

    c_env = os.environ.copy()
    c_env.pop("PYTEST_ADDOPTS", None)
    c_env.pop("FLAKY_ORDER_FILE", None)
    c_env.pop("FLAKY_EVENTS_FILE", None)
    c_env["PYTHONHASHSEED"] = "0"
    c_env["PYTHONDONTWRITEBYTECODE"] = "1"
    c_env["PYTHONPATH"] = target_pythonpath
    c_env["FLAKY_COLLECT_FILE"] = str(collect_tmp)

    collect_rc = _run_subprocess_with_timeout(
        [target_python, "-m", "pytest", *pytest_opts, "--collect-only", "--verbosity=-1", *extra_collect_args],
        cwd=project_dir,
        env=c_env,
        timeout_secs=timeout_secs,
        log_file=collect_log,
    )

    if collect_rc == 5:
        collect_tmp.unlink(missing_ok=True)
        if (project_dir / "scenario" / "tests").is_dir():
            say(f"hint: found benchmark suite at '{project_dir / 'scenario'}' — try: --project {project_dir / 'scenario'}")
        return die(f"pytest found no tests in {project_dir}", E_COLLECT)
    if collect_rc == 124:
        collect_tmp.unlink(missing_ok=True)
        return die(f"pytest collection timed out after {timeout_secs}s in {project_dir}", E_COLLECT)
    if collect_rc != 0:
        collect_tmp.unlink(missing_ok=True)
        try:
            tail_lines = collect_log.read_text(encoding="utf-8", errors="replace").splitlines()[-15:]
            for ln in tail_lines:
                sys.stderr.write(ln + "\n")
        except OSError:
            pass
        if (project_dir / "scenario" / "tests").is_dir():
            say(f"hint: found benchmark suite at '{project_dir / 'scenario'}' — try: --project {project_dir / 'scenario'}")
        return die(
            f"pytest could not collect the project (exit code {collect_rc}); full output in {out_rel}/collect.log",
            E_COLLECT,
        )

    collected_ids = []
    if collect_tmp.is_file():
        collected_ids = [
            ln.strip()
            for ln in collect_tmp.read_text(encoding="utf-8", errors="replace").splitlines()
            if ln.strip()
        ]
    if not collected_ids and collect_log.is_file():
        for ln in collect_log.read_text(encoding="utf-8", errors="replace").splitlines():
            s = ln.strip()
            if s.startswith("="):
                continue
            if not s:
                break
            if "::" in s:
                collected_ids.append(s)
    collect_tmp.unlink(missing_ok=True)

    if not collected_ids:
        return die(f"could not read any test IDs from {out_rel}/collect.log", E_COLLECT)

    shutil.rmtree(out_dir / "logs", ignore_errors=True)
    shutil.rmtree(out_dir / "orders", ignore_errors=True)
    for stale in ("results.csv", "run_meta.csv", "diagnosis.json", "report.md"):
        (out_dir / stale).unlink(missing_ok=True)
    (out_dir / "logs").mkdir(parents=True, exist_ok=True)
    (out_dir / "orders").mkdir(parents=True, exist_ok=True)

    collected_file = out_dir / "collected.txt"
    collected_file.write_text("".join(f"{nid}\n" for nid in collected_ids), encoding="utf-8")

    n_tests = len(collected_ids)
    n_seeds = len(seed_arr)
    n_order_runs = (orders + 1) if orders > 0 else 0
    n_runs = repeats + n_seeds + n_order_runs + n_tests * isolated
    say(f"collected {n_tests} tests; planned {n_runs} runs (timeout {timeout_secs}s each)")

    results_csv = out_dir / "results.csv"
    run_meta_csv = out_dir / "run_meta.csv"
    results_csv.write_text("run_id,condition,condition_value,position,test_id,outcome\n", encoding="utf-8")
    run_meta_csv.write_text(
        "run_id,condition,condition_value,hash_seed,exit_code,status,seconds,tests_expected,tests_reported,log_path\n",
        encoding="utf-8",
    )

    run_id = 0
    total_timeouts = 0
    total_crashes = 0
    total_parsed = 0
    start_ts = time.monotonic()

    def run_one(condition, value, hash_seed, expected_list, order_file=None, target_args=None):
        nonlocal run_id, total_timeouts, total_crashes, total_parsed
        run_id += 1
        if is_tty:
            sys.stderr.write(f"\r[detect] running {run_id}/{n_runs} ({condition} {value})...\033[K")
            sys.stderr.flush()
        pad_width = 5 if n_runs >= 1000 else 3
        log_rel = f"logs/run_{run_id:0{pad_width}d}.log"
        log_file = out_dir / log_rel

        tmp_run_dir = out_dir / f".tmp_run_{run_id}"
        tmp_run_dir.mkdir(parents=True, exist_ok=True)
        events_file = tmp_run_dir / "events.jsonl"

        r_env = os.environ.copy()
        r_env.pop("PYTEST_ADDOPTS", None)
        r_env.pop("FLAKY_COLLECT_FILE", None)
        r_env["PYTHONHASHSEED"] = str(hash_seed)
        r_env["PYTHONDONTWRITEBYTECODE"] = "1"
        r_env["PYTHONPATH"] = target_pythonpath
        r_env["TMPDIR"] = str(tmp_run_dir)
        r_env["TEMP"] = str(tmp_run_dir)
        r_env["TMP"] = str(tmp_run_dir)
        r_env["FLAKY_EVENTS_FILE"] = str(events_file)
        if order_file is not None:
            r_env["FLAKY_ORDER_FILE"] = str(order_file)
        else:
            r_env.pop("FLAKY_ORDER_FILE", None)

        cmd = [target_python, "-m", "pytest", *pytest_opts, *(target_args or [])]
        rc = _run_subprocess_with_timeout(
            cmd, cwd=project_dir, env=r_env, timeout_secs=timeout_secs, log_file=log_file
        )

        timed_out = False
        if rc in (0, 1):
            status = "ok"
        elif rc == 124:
            status = "timeout"
            timed_out = True
            total_timeouts += 1
            say(f"warning: run {run_id} ({condition} {value}) hit the {timeout_secs}s timeout")
        elif rc == 2:
            status = "interrupted"
        elif rc == 3:
            status = "internal_error"
        elif rc == 4:
            status = "usage_error"
        elif rc == 5:
            status = "no_tests"
        else:
            status = "crashed"

        if status not in ("ok", "timeout"):
            total_crashes += 1
            say(
                f"warning: run {run_id} ({condition} {value}) ended with pytest exit code {rc} ({status}); "
                f"see {out_rel}/{log_rel}"
            )

        rows, n_parsed = _events_or_log_to_rows(
            run_id, condition, value, timed_out, expected_list, events_file, log_file
        )
        shutil.rmtree(tmp_run_dir, ignore_errors=True)

        if rows:
            with results_csv.open("a", encoding="utf-8") as stream:
                stream.write("\n".join(rows) + "\n")

        n_expected = len(expected_list)
        total_parsed += n_parsed
        if status == "ok" and n_parsed < n_expected:
            total_crashes += 1
            say(
                f"warning: run {run_id} ({condition} {value}) reported only {n_parsed} of {n_expected} tests; "
                f"see {out_rel}/{log_rel}"
            )

        seconds_str = ""
        try:
            for ln in reversed(log_file.read_text(encoding="utf-8", errors="replace").splitlines()):
                m = _SUMMARY_SEC_RE.match(ln.strip())
                if m:
                    seconds_str = m.group(1)
                    break
        except OSError:
            pass

        with run_meta_csv.open("a", encoding="utf-8") as mstream:
            mstream.write(
                f"{run_id},{condition},{_csv_quote(value)},{hash_seed},{rc},{status},"
                f"{seconds_str},{n_expected},{n_parsed},{log_rel}\n"
            )

    for r in range(1, repeats + 1):
        run_one("repeat", str(r), 0, collected_ids, collected_file, extra_collect_args)
    say(f"repeat:   {repeats} runs done")

    for seed in seed_arr:
        run_one("hashseed", str(seed), seed, collected_ids, collected_file, extra_collect_args)
    say(f"hashseed: {n_seeds} runs done")

    if orders > 0:
        rev_ids = list(reversed(collected_ids))
        rev_file = out_dir / "orders" / "reverse.txt"
        rev_file.write_text("".join(f"{nid}\n" for nid in rev_ids), encoding="utf-8")
        run_one("order", "reverse", 0, rev_ids, rev_file, extra_collect_args)

        for k in range(1, orders + 1):
            shuf_ids = list(collected_ids)
            rng = random.Random(k)
            rng.shuffle(shuf_ids)
            shuf_file = out_dir / "orders" / f"shuffle_{k}.txt"
            shuf_file.write_text("".join(f"{nid}\n" for nid in shuf_ids), encoding="utf-8")
            run_one("order", f"shuffle:{k}", 0, shuf_ids, shuf_file, extra_collect_args)
        say(f"order:    {n_order_runs} runs done")

    if isolated > 0:
        single_file = out_dir / "orders" / "single.txt"
        iso_list = list(collected_ids)
        if isolated_scope == "smart":
            failed_set = set()
            with results_csv.open(encoding="utf-8") as rstream:
                reader = csv.DictReader(rstream)
                for row in reader:
                    if row["outcome"] not in ("passed", "skipped"):
                        failed_set.add(row["test_id"])
            iso_list = [nid for nid in collected_ids if nid in failed_set]
        iso_count = 0
        for test_id in iso_list:
            single_file.write_text(f"{test_id}\n", encoding="utf-8")
            test_file = test_id.split("::", 1)[0]
            target_arg = test_file if (project_dir / test_file).is_file() else test_id
            for _ in range(isolated):
                run_one("isolated", "alone", 0, [test_id], single_file, [target_arg])
                iso_count += 1
        single_file.unlink(missing_ok=True)
        say(f"isolated: {iso_count} runs done")

    elapsed = int(time.monotonic() - start_ts)
    raw_lines = 0
    for lf in (out_dir / "logs").glob("run_*.log"):
        try:
            raw_lines += len(lf.read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError:
            pass
    total_rows = max(0, len(results_csv.read_text(encoding="utf-8").splitlines()) - 1)

    say(f"finished {run_id} runs in {elapsed}s: {total_rows} result rows, {raw_lines} raw log lines")
    if total_timeouts:
        say(f"warning: {total_timeouts} runs timed out")
    if total_crashes:
        say(f"warning: {total_crashes} runs ended abnormally (see {out_rel}/run_meta.csv)")

    if total_parsed == 0:
        return die(
            f"no test results could be read from the logs; see {out_rel}/logs/ for what pytest printed",
            E_RUNS,
        )

    say(f"results:  {out_rel}/results.csv")
    say(f"run info: {out_rel}/run_meta.csv")
    say(f"raw logs: {out_rel}/logs/")

    if not do_report:
        say("--no-report given; stopping after results.csv")
        return 0

    return run_analysis_and_report()


def run_demo(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    _, _, tool_root, _, installed = _locate_paths()
    cwd = Path.cwd().resolve()

    if not installed and (tool_root / "scenario").is_dir():
        scenario = tool_root / "scenario"
        answer_key = tool_root / "docs" / "scenario.md"
        out_dir = tool_root / "out"
    elif (cwd / "scenario").is_dir():
        scenario = cwd / "scenario"
        answer_key = cwd / "docs" / "scenario.md"
        out_dir = cwd / "out"
    elif (cwd / "tests" / "test_cart.py").is_file() and (cwd / "src" / "ecommerce").is_dir():
        scenario = cwd
        answer_key = cwd.parent / "docs" / "scenario.md"
        out_dir = cwd / "out"
    else:
        scenario = cwd / "scenario"
        answer_key = cwd / "docs" / "scenario.md"
        out_dir = cwd / "out"

    python_bin = os.environ.get("FLAKY_TOOL_PYTHON") or sys.executable

    for idx, tok in enumerate(args):
        if tok in ("-h", "--help"):
            return run_detect(["--help"])
        if tok == "--out" and idx + 1 < len(args):
            out_dir = Path(args[idx + 1])
        elif tok.startswith("--out="):
            out_dir = Path(tok.split("=", 1)[1])
        elif tok == "--python" and idx + 1 < len(args):
            python_bin = args[idx + 1]
        elif tok.startswith("--python="):
            python_bin = tok.split("=", 1)[1]

    if not scenario.is_dir():
        sys.stderr.write(
            "demo.sh: error: scenario/ folder not found in current directory.\n"
            "To download the optional benchmark scenario on demand, run:\n"
            "  git clone --depth 1 --filter=blob:none --sparse "
            "https://github.com/Raintrrrreeeee/pytest-flaky-triage.git flaky-demo\n"
            "  cd flaky-demo\n"
            "  git sparse-checkout set --no-cone /scenario /docs\n"
            "  pytest-flaky-triage --demo\n"
        )
        return 2

    def heading(title):
        sys.stdout.write(f"\n== {title}\n")
        sys.stdout.flush()

    heading("The problem: the same suite, no code changes, run twice with plain pytest")
    for attempt in (1, 2):
        proc = subprocess.run(
            [python_bin, "-m", "pytest", "-q", "-p", "no:randomly", "-p", "no:cacheprovider"],
            cwd=str(scenario),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
        combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
        lines = [ln.strip() for ln in combined.splitlines() if ln.strip()]
        summary = lines[-1] if lines else "(no output)"
        sys.stdout.write(f"  run {attempt}: {summary}\n")
    sys.stdout.write("  Which failures are real bugs, which are flaky, and why? Plain pytest cannot say.\n")

    heading("Running detect on scenario/")
    rc = run_detect(["--project", str(scenario), "--out", str(out_dir), *args])
    if rc != 0:
        sys.stderr.write(f"demo.sh: error: detect failed with exit code {rc}\n")
        return rc

    out_dir = out_dir.resolve()
    try:
        out_rel = out_dir.relative_to(tool_root.resolve()).as_posix() if not installed else str(out_dir)
    except ValueError:
        out_rel = str(out_dir)

    results_csv = out_dir / "results.csv"
    if answer_key.is_file() and results_csv.is_file():
        heading(f"Passes per condition (from {out_rel}/results.csv) vs the answer key (docs/scenario.md)")
        expected_map = {}
        for ln in answer_key.read_text(encoding="utf-8", errors="replace").splitlines():
            if ln.startswith("| `test_"):
                parts = ln.split("`")
                if len(parts) >= 3:
                    expected_map[parts[1]] = parts[-2]

        order_names = []
        seen_names = set()
        totals = {}
        passed = {}
        with results_csv.open(encoding="utf-8") as rstream:
            reader = csv.DictReader(rstream)
            for row in reader:
                short = row["test_id"].split("::")[-1]
                cond = row["condition"]
                if short not in seen_names:
                    seen_names.add(short)
                    order_names.append(short)
                totals[(short, cond)] = totals.get((short, cond), 0) + 1
                if row["outcome"] == "passed":
                    passed[(short, cond)] = passed.get((short, cond), 0) + 1

        sys.stdout.write(
            f"  {'test':<46} {'expected':<15} {'repeat':>9} {'hashseed':>9} {'order':>9} {'isolated':>9}\n"
        )
        for name in order_names:
            want = expected_map.get(name, "(not in key)")
            cells = []
            for cond in ("repeat", "hashseed", "order", "isolated"):
                key = (name, cond)
                if key in totals:
                    cells.append(f"{passed.get(key, 0)}/{totals[key]}")
                else:
                    cells.append("-")
            sys.stdout.write(
                f"  {name:<46} {want:<15} {cells[0]:>9} {cells[1]:>9} {cells[2]:>9} {cells[3]:>9}\n"
            )

    raw_lines = 0
    for lf in (out_dir / "logs").glob("run_*.log"):
        try:
            raw_lines += len(lf.read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError:
            pass
    rows = max(0, len(results_csv.read_text(encoding="utf-8").splitlines()) - 1) if results_csv.is_file() else 0

    heading("Outputs")
    sys.stdout.write(f"  raw pytest logs:  {out_rel}/logs/ ({raw_lines} lines)\n")
    sys.stdout.write(f"  structured data:  {out_rel}/results.csv ({rows} rows), {out_rel}/run_meta.csv\n")
    if (out_dir / "diagnosis.json").is_file():
        sys.stdout.write(f"  diagnosis:        {out_rel}/diagnosis.json\n")
    if (out_dir / "report.md").is_file():
        sys.stdout.write(f"  triage report:    {out_rel}/report.md\n")
    return 0
