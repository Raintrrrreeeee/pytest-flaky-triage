"""Tests for Stage 1 single-source runtime packaging and runner regressions."""

from pathlib import Path
import subprocess
import sys

import pytest

from flaky_triage import cli, runner

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
RUNTIME_DIR = REPO_ROOT / "src" / "flaky_triage" / "runtime"


def test_scripts_delegate_to_runtime_single_source():
    """scripts/ wrappers must delegate to src/flaky_triage/runtime/ without duplicating logic."""
    for rel in ("detect.sh", "demo.sh", "lib/flaky_plugin.py", "lib/parse_log.awk"):
        assert (RUNTIME_DIR / rel).is_file(), f"missing canonical runtime file: {rel}"
        assert (SCRIPTS_DIR / rel).is_file(), f"missing scripts entry point: {rel}"

    detect_wrapper = (SCRIPTS_DIR / "detect.sh").read_text(encoding="utf-8")
    demo_wrapper = (SCRIPTS_DIR / "demo.sh").read_text(encoding="utf-8")
    plugin_wrapper = (SCRIPTS_DIR / "lib" / "flaky_plugin.py").read_text(encoding="utf-8")

    assert "src/flaky_triage/runtime/detect.sh" in detect_wrapper
    assert len(detect_wrapper.splitlines()) < 25
    assert "src/flaky_triage/runtime/demo.sh" in demo_wrapper
    assert len(demo_wrapper.splitlines()) < 25
    assert "flaky_plugin.py" in plugin_wrapper
    assert len(plugin_wrapper.splitlines()) < 30

    assert (SCRIPTS_DIR / "lib" / "parse_log.awk").read_bytes() == (
        RUNTIME_DIR / "lib" / "parse_log.awk"
    ).read_bytes()

    _, _, _, lib_dir, _ = runner._locate_paths()
    assert lib_dir == RUNTIME_DIR / "lib"

    res_scripts = subprocess.run(
        ["bash", str(SCRIPTS_DIR / "detect.sh"), "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    res_runtime = subprocess.run(
        ["bash", str(RUNTIME_DIR / "detect.sh"), "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert res_scripts.stdout == res_runtime.stdout


def test_parse_verbose_log_fallback_supports_spaces_in_test_paths(tmp_path):
    """_parse_verbose_log_fallback and _events_or_log_to_rows must parse paths with spaces."""
    expected_ids = [
        "tests/sub dir/test_space.py::test_case",
        "tests/my suite/test_param.py::test_val[hello world]",
        "tests/sub dir/test_space.py::test_skip",
        "tests/sub dir/test_space.py::test_teardown_err",
        "tests/sub dir/test_space.py::test_missing",
    ]
    log_content = "\n".join(
        [
            "============================= test session starts =============================",
            "tests/sub dir/test_space.py::test_case PASSED                            [ 25%]",
            "tests/my suite/test_param.py::test_val[hello world] FAILED               [ 50%]",
            "tests/sub dir/test_space.py::test_skip SKIPPED (conditional skip)        [ 75%]",
            "tests/sub dir/test_space.py::test_teardown_err PASSED                    [100%]",
            "tests/sub dir/test_space.py::test_teardown_err ERROR                     [100%]",
            "=================================== FAILURES ===================================",
            "tests/sub dir/test_space.py::test_missing PASSED",
            "=========================== short test summary info ===========================",
        ]
    )
    outcomes, positions = runner._parse_verbose_log_fallback(log_content, expected_ids)
    assert outcomes == {
        "tests/sub dir/test_space.py::test_case": "passed",
        "tests/my suite/test_param.py::test_val[hello world]": "failed",
        "tests/sub dir/test_space.py::test_skip": "skipped",
        "tests/sub dir/test_space.py::test_teardown_err": "error",
    }
    assert positions == {
        "tests/sub dir/test_space.py::test_case": 1,
        "tests/my suite/test_param.py::test_val[hello world]": 2,
        "tests/sub dir/test_space.py::test_skip": 3,
        "tests/sub dir/test_space.py::test_teardown_err": 4,
    }

    log_file = tmp_path / "run_001.log"
    log_file.write_text(log_content, encoding="utf-8")
    missing_events = tmp_path / "events.jsonl"
    rows, n_parsed = runner._events_or_log_to_rows(
        1, "repeat", "1", False, expected_ids, missing_events, log_file
    )
    assert n_parsed == 4
    assert rows[0] == '1,repeat,"1",1,"tests/sub dir/test_space.py::test_case",passed'
    assert rows[1] == '1,repeat,"1",2,"tests/my suite/test_param.py::test_val[hello world]",failed'
    assert rows[2] == '1,repeat,"1",3,"tests/sub dir/test_space.py::test_skip",skipped'
    assert rows[3] == '1,repeat,"1",4,"tests/sub dir/test_space.py::test_teardown_err",error'
    assert rows[4] == '1,repeat,"1",,"tests/sub dir/test_space.py::test_missing",not_run'


def test_runner_cleans_up_temp_files_on_keyboard_interrupt(tmp_path, monkeypatch):
    """runner.run_detect must clean up .tmp_run_*, collected.tmp, single.txt, and smart_isolated.txt on Ctrl+C."""
    proj = tmp_path / "proj"
    (proj / "tests").mkdir(parents=True)
    (proj / "tests" / "test_sample.py").write_text(
        "def test_fail():\n    assert False\n",
        encoding="utf-8",
    )
    out_dir = tmp_path / "out"

    orig_run_subproc = runner._run_subprocess_with_timeout
    call_count = {"n": 0}

    def interrupt_on_isolated(cmd, cwd, env, timeout_secs, log_file):
        call_count["n"] += 1
        # Call 1 = collection, Call 2 = repeat 1, Call 3 = isolated run 1
        if call_count["n"] == 3:
            # Verify transient files exist right before KeyboardInterrupt
            assert (out_dir / ".tmp_run_2").is_dir()
            assert (out_dir / "orders" / "single.txt").is_file()
            assert (out_dir / "orders" / "smart_isolated.txt").is_file()
            (out_dir / "collected.tmp").write_text("stale\n", encoding="utf-8")
            raise KeyboardInterrupt("simulated Ctrl+C")
        return orig_run_subproc(cmd, cwd, env, timeout_secs, log_file)

    monkeypatch.setattr(runner, "_run_subprocess_with_timeout", interrupt_on_isolated)

    with pytest.raises(KeyboardInterrupt):
        runner.run_detect(
            [
                "--project", str(proj),
                "--out", str(out_dir),
                "--repeats", "1",
                "--hash-seeds", "",
                "--orders", "0",
                "--timezones", "",
                "--isolated", "1",
                "--isolated-scope", "smart",
                "--no-report",
                "--python", sys.executable,
            ]
        )

    assert list(out_dir.glob(".tmp_run_*")) == []
    assert not (out_dir / "collected.tmp").exists()
    assert not (out_dir / "orders" / "single.txt").exists()
    assert not (out_dir / "orders" / "smart_isolated.txt").exists()


def test_detect_sh_and_runner_smart_isolated_run_counts(tmp_path, capsys):
    """detect.sh and runner.py in smart isolated mode must report base suite runs and only isolate failing tests."""
    proj = tmp_path / "proj"
    sub = proj / "tests" / "sub dir"
    sub.mkdir(parents=True)
    (sub / "test_space.py").write_text(
        "def test_pass_1():\n    assert True\n\n"
        "def test_pass_2():\n    assert True\n\n"
        "def test_fail_1():\n    assert False\n",
        encoding="utf-8",
    )

    out_sh = tmp_path / "out_sh"
    res = subprocess.run(
        [
            "bash",
            str(SCRIPTS_DIR / "detect.sh"),
            "--project", str(proj),
            "--out", str(out_sh),
            "--repeats", "2",
            "--hash-seeds", "",
            "--orders", "0",
            "--timezones", "",
            "--isolated", "1",
            "--isolated-scope", "smart",
            "--no-report",
            "--python", sys.executable,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert (
        "collected 3 tests; planned up to 5 runs (2 suite runs + smart isolated for failing tests; timeout 60s each)"
        in res.stderr
    )
    assert "isolated: 1 runs done" in res.stderr
    assert "finished 3 runs in " in res.stderr

    out_py = tmp_path / "out_py"
    rc = runner.run_detect(
        [
            "--project", str(proj),
            "--out", str(out_py),
            "--repeats", "2",
            "--hash-seeds", "",
            "--orders", "0",
            "--timezones", "",
            "--isolated", "1",
            "--isolated-scope", "smart",
            "--no-report",
            "--python", sys.executable,
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert (
        "collected 3 tests; planned up to 5 runs (2 suite runs + smart isolated for failing tests; timeout 60s each)"
        in captured.err
    )
    assert "isolated: 1 runs done" in captured.err
    assert "finished 3 runs in " in captured.err


def test_run_demo_fallback_path_matches_demo_sh(tmp_path, monkeypatch):
    """run_demo() else branch must fall back to tool_root / 'scenario' like demo.sh."""
    fake_cwd = tmp_path / "empty_cwd"
    fake_cwd.mkdir()
    monkeypatch.chdir(fake_cwd)
    monkeypatch.setenv("FLAKY_INSTALLED_PACKAGE", "1")

    called = {}

    def fake_run_detect(argv):
        called["argv"] = argv
        return 0

    monkeypatch.setattr(runner, "run_detect", fake_run_detect)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a[0], 0, stdout="1 passed in 0.01s\n", stderr=""),
    )

    out_dir = tmp_path / "demo_out"
    rc = runner.run_demo(["--out", str(out_dir), "--no-report"])
    assert rc == 0
    assert called["argv"][:2] == ["--project", str(REPO_ROOT / "scenario")]
