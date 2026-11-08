"""Sideband pytest hook plugin and runner helper utilities for Stage 1.

When loaded as a pytest plugin (`pytest -p flaky_plugin`), it pops internal
`FLAKY_*` environment variables during `pytest_configure` so child processes
spawned by tests under test are never polluted, records exact collected node IDs,
applies deterministic in-memory test ordering, and streams structured per-test
lifecycle events (`start`, `report`, `finish`) to a sideband JSONL file.

When invoked as a CLI script (`python flaky_plugin.py <subcommand>`), it provides:
  - `run-timeout <secs> <cmd...>`: process-group timeout & signal controller
  - `reverse <in_file> <out_file>`: reverse collected node IDs
  - `shuffle <in_file> <seed> <out_file>`: 64-bit Mersenne Twister Fisher-Yates shuffle
  - `events-to-csv <run_id> <cond> <cval> <timed_out> <expected_file> <events_file>`:
    convert sideband JSONL events to `results.csv` rows
"""

import json
import os
from pathlib import Path
import random
import signal
import subprocess
import sys

_STATE = {
    "collect_file": None,
    "order_file": None,
    "events_file": None,
    "events_handle": None,
}

_RANK = {
    "error": 3,
    "failed": 2,
    "passed": 1,
    "skipped": 1,
    "xfailed": 1,
    "xpassed": 1,
}


def _emit_event(payload):
    handle = _STATE.get("events_handle")
    if handle is not None:
        try:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
            handle.flush()
        except OSError:
            pass


def pytest_configure(config):
    """Pop FLAKY_* env vars immediately so subprocesses spawned by tests are clean."""
    collect_file = os.environ.pop("FLAKY_COLLECT_FILE", None)
    order_file = os.environ.pop("FLAKY_ORDER_FILE", None)
    events_file = os.environ.pop("FLAKY_EVENTS_FILE", None)

    _STATE["collect_file"] = collect_file
    _STATE["order_file"] = order_file
    _STATE["events_file"] = events_file

    if events_file:
        try:
            Path(events_file).parent.mkdir(parents=True, exist_ok=True)
            _STATE["events_handle"] = open(events_file, "w", encoding="utf-8")
        except OSError:
            _STATE["events_handle"] = None

    # Neutralize maxfail or xdist options if injected by project conftest
    if hasattr(config, "option"):
        if hasattr(config.option, "maxfail"):
            config.option.maxfail = 0
        if hasattr(config.option, "numprocesses"):
            config.option.numprocesses = None


def pytest_unconfigure(config):
    handle = _STATE.get("events_handle")
    if handle is not None:
        try:
            handle.close()
        except OSError:
            pass
        _STATE["events_handle"] = None


def pytest_collection_modifyitems(session, config, items):
    order_file = _STATE.get("order_file")
    if order_file:
        try:
            wanted = [
                line.rstrip("\r\n")
                for line in Path(order_file).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            by_id = {item.nodeid: item for item in items}
            items[:] = [by_id[nid] for nid in wanted if nid in by_id]
        except OSError:
            pass

    collect_file = _STATE.get("collect_file")
    if collect_file:
        try:
            Path(collect_file).parent.mkdir(parents=True, exist_ok=True)
            with open(collect_file, "w", encoding="utf-8") as stream:
                for item in items:
                    stream.write(f"{item.nodeid}\n")
        except OSError:
            pass


def pytest_runtest_logstart(nodeid, location):
    _emit_event({"event": "start", "nodeid": nodeid})


def pytest_runtest_logreport(report):
    outcome = None
    has_xfail = hasattr(report, "wasxfail")
    if report.when == "setup":
        if report.failed:
            outcome = "error"
        elif report.skipped:
            outcome = "xfailed" if has_xfail else "skipped"
    elif report.when == "call":
        if report.passed:
            outcome = "xpassed" if has_xfail else "passed"
        elif report.failed:
            outcome = "xfailed" if has_xfail else "failed"
        elif report.skipped:
            outcome = "xfailed" if has_xfail else "skipped"
    elif report.when == "teardown":
        if report.failed:
            outcome = "error"

    if outcome is not None:
        _emit_event({
            "event": "report",
            "nodeid": report.nodeid,
            "when": report.when,
            "outcome": outcome,
        })


def pytest_runtest_logfinish(nodeid, location):
    _emit_event({"event": "finish", "nodeid": nodeid})


# ---------------------------------------------------------------- CLI helpers


def _csv_quote(text):
    escaped = str(text).replace('"', '""')
    return f'"{escaped}"'


def _cmd_run_timeout(argv):
    timeout_secs = float(argv[0])
    cmd = argv[1:]
    is_win = os.name == "nt"
    popen_kwargs = {"stdin": subprocess.DEVNULL}
    if is_win:
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, **popen_kwargs)

    def _kill_tree(force=False, sig=None):
        if is_win:
            try:
                args = ["taskkill", "/T", "/PID", str(proc.pid)]
                if force:
                    args.insert(1, "/F")
                subprocess.run(
                    args,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            except OSError:
                pass
            try:
                if force:
                    proc.kill()
                else:
                    proc.terminate()
            except OSError:
                pass
        else:
            use_sig = sig if sig is not None else (signal.SIGKILL if force else signal.SIGTERM)
            try:
                os.killpg(proc.pid, use_sig)
            except ProcessLookupError:
                pass

    def _forward_signal(signum, _frame):
        _kill_tree(force=False, sig=signum)

    signal.signal(signal.SIGINT, _forward_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _forward_signal)

    try:
        rc = proc.wait(timeout=timeout_secs)
        return (128 + (-rc)) if rc < 0 else rc
    except subprocess.TimeoutExpired:
        _kill_tree(force=False)
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            _kill_tree(force=True)
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                pass
        return 124


def _cmd_reverse(argv):
    in_path, out_path = Path(argv[0]), Path(argv[1])
    lines = [
        line.rstrip("\r\n")
        for line in in_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    lines.reverse()
    out_path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    return 0


def _cmd_shuffle(argv):
    in_path, seed, out_path = Path(argv[0]), int(argv[1]), Path(argv[2])
    lines = [
        line.rstrip("\r\n")
        for line in in_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    rng = random.Random(seed)
    rng.shuffle(lines)
    out_path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    return 0


def _cmd_events_to_csv(argv):
    run_id, condition, cvalue, timed_out_str, expected_path, events_path = argv
    timed_out = int(timed_out_str) == 1
    exp_file = Path(expected_path)
    ev_file = Path(events_path)
    if not ev_file.is_file():
        return 2

    expected_order = []
    expected_set = set()
    for line in exp_file.read_text(encoding="utf-8").splitlines():
        nid = line.rstrip("\r\n")
        if nid and nid not in expected_set:
            expected_set.add(nid)
            expected_order.append(nid)

    outcomes = {}
    positions = {}
    started = set()
    finished = set()
    n_reported = 0
    any_event = False

    with ev_file.open(encoding="utf-8") as stream:
        for raw_line in stream:
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

    if not any_event:
        return 2

    # If the run timed out while a test was in teardown (started and reported
    # call outcome, but never emitted finish), attribute the timeout to that test.
    teardown_timeout_id = None
    if timed_out:
        for nid in expected_order:
            if nid in started and nid not in finished and nid in outcomes:
                teardown_timeout_id = nid
                break

    missing_seen = False
    for nid in expected_order:
        if nid == teardown_timeout_id:
            out = "timeout"
            pos = ""
            missing_seen = True
        elif nid in outcomes:
            out = outcomes[nid]
            pos = str(positions[nid])
        else:
            out = "timeout" if (timed_out and not missing_seen) else "not_run"
            missing_seen = True
            pos = ""
        print(f"{run_id},{condition},{_csv_quote(cvalue)},{pos},{_csv_quote(nid)},{out}")
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        return 2
    sub = argv[0]
    rest = argv[1:]
    if sub == "run-timeout":
        return _cmd_run_timeout(rest)
    if sub == "reverse":
        return _cmd_reverse(rest)
    if sub == "shuffle":
        return _cmd_shuffle(rest)
    if sub == "events-to-csv":
        return _cmd_events_to_csv(rest)
    return 2


if __name__ == "__main__":
    sys.exit(main())
