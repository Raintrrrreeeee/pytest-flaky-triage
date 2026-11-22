"""Argument and failure handling of scripts/detect.sh and flaky_triage.runner (exit codes in README.md)."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DETECT = ROOT / "scripts" / "detect.sh"
FIXTURES = ROOT / "tests" / "fixtures" / "parser"
GOOD_PROJECT = str(FIXTURES / "outcomes")


def detect(*args):
    if os.name == "nt" or os.environ.get("FLAKY_PURE_PYTHON") == "1":
        env = os.environ.copy()
        env["FLAKY_PURE_PYTHON"] = "1"
        env["FLAKY_CLI_NAME"] = "scripts/detect.sh"
        env["PYTHONPATH"] = os.pathsep.join(
            [str(ROOT / "src"), *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])]
        )
        return subprocess.run(
            [sys.executable, "-m", "flaky_triage.cli", *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
    return subprocess.run(["bash", str(DETECT), *args], capture_output=True, text=True, timeout=120)


@pytest.mark.parametrize(
    "args, message",
    [
        ([], "--project is required"),
        (["--project", "/no/such/folder"], "does not exist"),
        (["--project", GOOD_PROJECT, "--repeats", "0"], "--repeats must be"),
        (["--project", GOOD_PROJECT, "--repeats", "ten"], "--repeats must be"),
        (["--project", GOOD_PROJECT, "--repeats", "-3"], "--repeats must be"),
        (["--project", GOOD_PROJECT, "--orders", "1.5"], "--orders must be"),
        (["--project", GOOD_PROJECT, "--timeout", "0"], "--timeout must be"),
        (["--project", GOOD_PROJECT, "--hash-seeds", "1 two"], "--hash-seeds values"),
        (["--project", GOOD_PROJECT, "--timezones", "UTC bad;tz"], "--timezones values"),
        (["--project", GOOD_PROJECT, "--out", "/"], "--out must not be / or HOME"),
        (["--project", GOOD_PROJECT, "--out", str(ROOT)], "--out must be a sub-folder"),
        (["--project", GOOD_PROJECT, "--isolated-scope", "invalid"], "--isolated-scope must be"),
        (["--project", GOOD_PROJECT, "--isolated-runs", "0"], "--isolated-runs must be"),
        (["--project", GOOD_PROJECT, "--" + "fast"], "unknown option"),
        (["--project", GOOD_PROJECT, "--frobnicate"], "unknown option"),
        (["--project"], "needs a value"),
    ],
)
def test_bad_arguments_exit_2(args, message):
    result = detect(*args)
    assert result.returncode == 2
    assert message in result.stderr


def test_missing_pytest_exits_3(tmp_path):
    result = detect("--project", GOOD_PROJECT, "--out", str(tmp_path / "runner"), "--python", "/usr/bin/false")
    assert result.returncode == 3
    assert "pytest is not installed" in result.stderr


@pytest.mark.parametrize("case, message", [
    ("collection_error", "could not collect"),
    ("no_tests", "found no tests"),
])
def test_collection_problems_exit_4(tmp_path, case, message):
    result = detect("--project", str(FIXTURES / case), "--out", str(tmp_path / "runner"), "--no-report")
    assert result.returncode == 4
    assert message in result.stderr


def test_failed_collection_keeps_previous_results(tmp_path):
    out = str(tmp_path / "runner_keep")
    first = detect("--project", GOOD_PROJECT, "--out", out, "--no-report",
                   "--repeats", "1", "--hash-seeds", "", "--orders", "0", "--isolated", "0")
    assert first.returncode == 0, first.stderr
    before = (Path(out) / "results.csv").read_text()

    second = detect("--project", str(FIXTURES / "no_tests"), "--out", out, "--no-report")
    assert second.returncode == 4
    assert (Path(out) / "results.csv").read_text() == before


def test_symlinked_output_outside_tool_is_rejected(tmp_path):
    external = tmp_path / "external"
    logs = external / "logs"
    logs.mkdir(parents=True)
    sentinel = logs / "sentinel.txt"
    sentinel.write_text("must survive runner validation\n")

    link = tmp_path / "external_link"
    if link.exists() or link.is_symlink():
        link.unlink()
    try:
        link.symlink_to(external, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks not permitted in this environment: {exc}")
    try:
        result = detect("--project", GOOD_PROJECT, "--out", str(link), "--no-report")
    finally:
        link.unlink(missing_ok=True)

    assert result.returncode == 2
    assert "--out must not be a symlink" in result.stderr
    assert sentinel.read_text() == "must survive runner validation\n"


def test_symlink_alias_of_the_project_is_rejected(tmp_path):
    link = tmp_path / "link_to_project"
    link.unlink(missing_ok=True)
    try:
        link.symlink_to(FIXTURES / "outcomes", target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks not permitted in this environment: {exc}")
    try:
        result = detect("--project", GOOD_PROJECT, "--out", str(link), "--no-report")
    finally:
        link.unlink(missing_ok=True)
    assert result.returncode == 2
    assert "must not be" in result.stderr


def test_external_output_directory_and_stale_artifact_cleanup(tmp_path):
    ext_out = tmp_path / "external_out"
    ext_out.mkdir(parents=True)
    stale_diag = ext_out / "diagnosis.json"
    stale_report = ext_out / "report.md"
    stale_diag.write_text("stale", encoding="utf-8")
    stale_report.write_text("stale", encoding="utf-8")

    result = detect(
        "--project", GOOD_PROJECT, "--out", str(ext_out), "--no-report",
        "--repeats", "1", "--hash-seeds", "", "--orders", "0", "--isolated", "0",
    )
    assert result.returncode == 0, result.stderr
    assert (ext_out / "results.csv").is_file()
    assert not stale_diag.exists()
    assert not stale_report.exists()


def test_output_folder_containing_the_project_is_rejected():
    # --out tests/fixtures/parser would clean up around the fixture projects
    result = detect("--project", GOOD_PROJECT, "--out", str(FIXTURES), "--no-report")
    assert result.returncode == 2
    assert "must not contain the project folder" in result.stderr


def test_leading_zero_numbers_are_decimal(tmp_path):
    # 08 is not valid octal; Bash arithmetic would reject it without 10#
    result = detect("--project", GOOD_PROJECT, "--out", str(tmp_path / "runner"), "--no-report",
                    "--repeats", "08", "--hash-seeds", "", "--orders", "0", "--timezones", "", "--isolated", "0")
    assert result.returncode == 0, result.stderr
    assert "planned 8 runs" in result.stderr


def test_help_exits_0():
    result = detect("--help")
    assert result.returncode == 0
    assert "Usage: scripts/detect.sh" in result.stdout


def test_equals_syntax_and_report_only_mode(tmp_path):
    out_dir = tmp_path / "eq_out"
    res = detect(
        f"--project={GOOD_PROJECT}",
        f"--out={out_dir}",
        "--repeats=2",
        "--hash-seeds=",
        "--orders=0",
        "--isolated=0",
        "--lang=en",
    )
    assert res.returncode == 0, res.stderr
    assert "# Test-Stability Triage Report" in (out_dir / "report.md").read_text(encoding="utf-8")

    # Now switch language to Chinese with --report-only without re-running pytest
    res_zh = detect(
        f"--project={GOOD_PROJECT}",
        f"--out={out_dir}",
        "--lang=zh",
        "--report-only",
    )
    assert res_zh.returncode == 0, res_zh.stderr
    assert "# 测试稳定性诊断报告" in (out_dir / "report.md").read_text(encoding="utf-8")

    # --report-only on an empty output directory exits 6
    empty_out = tmp_path / "empty_out"
    res_empty = detect(f"--project={GOOD_PROJECT}", f"--out={empty_out}", "--report-only")
    assert res_empty.returncode == 6


def test_sparse_checkout_wrapper_dir_hints_scenario_subfolder(tmp_path):
    wrapper = tmp_path / "flaky-demo"
    (wrapper / "scenario" / "tests").mkdir(parents=True)
    (wrapper / "scenario" / "src").mkdir(parents=True)
    (wrapper / "scenario" / "src" / "ecommerce.py").write_text("VAL = 1\n", encoding="utf-8")
    (wrapper / "scenario" / "tests" / "test_sample.py").write_text(
        "import ecommerce\ndef test_ok():\n    assert ecommerce.VAL == 1\n", encoding="utf-8"
    )
    res = detect("--project", str(wrapper), "--out", str(tmp_path / "out"), "--no-report")
    assert res.returncode == 4
    assert "--project" in res.stderr and "scenario" in res.stderr


def test_installed_cli_branding_and_demo_mode(tmp_path):
    env_PYTHONPATH = str(ROOT / "src")
    proc_help = subprocess.run(
        [sys.executable, "-m", "flaky_triage.cli", "--help"],
        env={**os.environ, "PYTHONPATH": env_PYTHONPATH},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc_help.returncode == 0
    assert "Usage: pytest-flaky-triage" in proc_help.stdout

    # Simulate installed package mode (--demo when scenario is not in cwd prints helpful download instructions)
    use_pure = os.name == "nt" or os.environ.get("FLAKY_PURE_PYTHON") == "1"
    runtime_demo = ROOT / "src" / "flaky_triage" / "runtime" / "demo.sh"
    assert runtime_demo.is_file()
    demo_cmd = (
        [sys.executable, "-m", "flaky_triage.cli", "--demo"]
        if use_pure
        else ["bash", str(runtime_demo)]
    )
    proc_missing_demo = subprocess.run(
        demo_cmd,
        cwd=str(tmp_path),
        env={
            **os.environ,
            "FLAKY_INSTALLED_PACKAGE": "1",
            "FLAKY_PURE_PYTHON": "1" if use_pure else "0",
            "PYTHONPATH": env_PYTHONPATH,
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc_missing_demo.returncode == 2
    assert "git sparse-checkout" in proc_missing_demo.stderr

    # When ./scenario exists in cwd, installed --demo runs against ./scenario and writes ./out
    demo_workspace = tmp_path / "demo_ws"
    (demo_workspace / "scenario" / "tests").mkdir(parents=True)
    (demo_workspace / "scenario" / "tests" / "test_tiny.py").write_text(
        "def test_pass():\n    assert True\n", encoding="utf-8"
    )
    proc_run_demo = subprocess.run(
        [
            *demo_cmd,
            "--repeats=2",
            "--hash-seeds=",
            "--orders=0",
            "--isolated=0",
        ],
        cwd=str(demo_workspace),
        env={
            **os.environ,
            "FLAKY_INSTALLED_PACKAGE": "1",
            "FLAKY_PURE_PYTHON": "1" if use_pure else "0",
            "FLAKY_TOOL_PYTHON": sys.executable,
            "PYTHONPATH": env_PYTHONPATH,
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc_run_demo.returncode == 0, proc_run_demo.stderr
    assert (demo_workspace / "out" / "report.md").is_file()

    # Running with --out inside --project (e.g. cd proj && pytest-flaky-triage --project .)
    # ignores the output folder during pytest collection
    proj_dir = demo_workspace / "scenario"
    in_proj_out = proj_dir / "out"
    in_proj_out.mkdir(parents=True, exist_ok=True)
    (in_proj_out / "test_should_be_ignored.py").write_text(
        "def test_fail():\n    assert False\n", encoding="utf-8"
    )
    res_in_proj = detect(
        "--project",
        str(proj_dir),
        "--out",
        str(in_proj_out),
        "--repeats=2",
        "--hash-seeds=",
        "--orders=0",
        "--isolated=0",
    )
    assert res_in_proj.returncode == 0, res_in_proj.stderr
    collected_ids = (in_proj_out / "collected.txt").read_text(encoding="utf-8")
    assert "test_should_be_ignored" not in collected_ids


def test_pure_python_runner_cross_platform_mode(tmp_path):
    """Explicitly exercise flaky_triage.runner (used natively on Windows PowerShell/CMD)."""
    env = {
        **os.environ,
        "FLAKY_PURE_PYTHON": "1",
        "PYTHONPATH": str(ROOT / "src"),
    }
    out_dir = tmp_path / "pure_out"
    res = subprocess.run(
        [
            sys.executable,
            "-m",
            "flaky_triage",
            f"--project={GOOD_PROJECT}",
            f"--out={out_dir}",
            "--repeats=2",
            "--hash-seeds=1",
            "--orders=1",
            "--isolated=1",
            "--lang=zh",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert res.returncode == 0, res.stderr
    assert (out_dir / "results.csv").is_file()
    assert (out_dir / "diagnosis.json").is_file()
    report_text = (out_dir / "report.md").read_text(encoding="utf-8")
    assert "# 测试稳定性诊断报告" in report_text


def test_intensive_preset_and_isolated_runs(tmp_path):
    """Verify --intensive preset with explicit overrides and multi-round --isolated-runs."""
    import csv

    help_res = detect("--help")
    assert help_res.returncode == 0
    assert "--intensive" in help_res.stdout
    assert "--isolated-runs" in help_res.stdout

    tiny_proj = tmp_path / "tiny_proj"
    (tiny_proj / "tests").mkdir(parents=True)
    (tiny_proj / "tests" / "test_tiny.py").write_text(
        "def test_a():\n    assert True\n\ndef test_b():\n    assert True\n",
        encoding="utf-8",
    )
    out_dir = tmp_path / "intensive_out"
    res = detect(
        "--project",
        str(tiny_proj),
        "--out",
        str(out_dir),
        "--intensive",
        "--repeats",
        "2",
        "--hashseeds",
        "1",
        "--shuffles",
        "1",
        "--timezones",
        "UTC",
        "--isolated-runs",
        "2",
        "--no-report",
    )
    assert res.returncode == 0, res.stderr
    with (out_dir / "results.csv").open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    iso_a = [r for r in rows if r["condition"] == "isolated" and r["test_id"].endswith("::test_a")]
    iso_b = [r for r in rows if r["condition"] == "isolated" and r["test_id"].endswith("::test_b")]
    assert len(iso_a) == 2
    assert len(iso_b) == 2
    assert len({r["run_id"] for r in iso_a}) == 2
    assert len({r["run_id"] for r in iso_b}) == 2


def _run_both_engines(args):
    """Run detect with both Pure Python runner and (on POSIX) Bash detect.sh."""
    outcomes = []
    env_py = os.environ.copy()
    env_py["FLAKY_PURE_PYTHON"] = "1"
    env_py["FLAKY_CLI_NAME"] = "scripts/detect.sh"
    env_py["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT / "src"), *([env_py["PYTHONPATH"]] if env_py.get("PYTHONPATH") else [])]
    )
    outcomes.append(
        (
            "python",
            subprocess.run(
                [sys.executable, "-m", "flaky_triage.cli", *args],
                env=env_py,
                capture_output=True,
                text=True,
                timeout=120,
            ),
        )
    )
    if os.name != "nt":
        outcomes.append(
            (
                "bash",
                subprocess.run(
                    ["bash", str(DETECT), *args],
                    capture_output=True,
                    text=True,
                    timeout=120,
                ),
            )
        )
    return outcomes


def test_task1_addopts_override_ignores_hostile_project_config(tmp_path):
    """Task 1: -o addopts="" shields collection and matrix runs from hostile project addopts."""
    proj = tmp_path / "hostile_addopts_proj"
    (proj / "tests").mkdir(parents=True)
    (proj / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\naddopts = "-n auto --cov=nonexistent --strict-config --unknown-xyz-flag"\n',
        encoding="utf-8",
    )
    (proj / "tests" / "test_ok.py").write_text(
        "def test_ok():\n    assert 1 + 1 == 2\n",
        encoding="utf-8",
    )
    for engine, res in _run_both_engines(
        [
            "--project", str(proj),
            "--out", str(tmp_path / f"out_{os.getpid()}" / "run"),
            "--repeats", "1",
            "--hash-seeds", "",
            "--orders", "0",
            "--timezones", "",
            "--isolated", "0",
            "--no-report",
        ]
    ):
        assert res.returncode == 0, f"{engine} failed: {res.stderr}"


def test_task2_timezone_validation_and_pacific_pago_pago_default(tmp_path):
    """Task 2: Invalid IANA timezones (including America/Pago_Pago) exit with 2, and default uses Pacific/Pago_Pago."""
    import csv

    for bad_tz in ("America/Pago_Pago", "Mars/Olympus_Mons"):
        for engine, res in _run_both_engines(
            [
                "--project", GOOD_PROJECT,
                "--out", str(tmp_path / f"bad_tz_{bad_tz.replace('/', '_')}"),
                "--timezones", f"UTC {bad_tz}",
                "--no-report",
            ]
        ):
            assert res.returncode == 2, f"{engine} did not reject {bad_tz}"
            assert "--timezones values must be valid timezone identifiers" in res.stderr
            assert bad_tz in res.stderr

    proj = tmp_path / "tz_proj"
    (proj / "tests").mkdir(parents=True)
    (proj / "tests" / "test_tz.py").write_text("def test_pass():\n    assert True\n", encoding="utf-8")
    for engine in ("python", *(["bash"] if os.name != "nt" else [])):
        out_dir = tmp_path / f"tz_default_{engine}"
        args = [
            "--project", str(proj),
            "--out", str(out_dir),
            "--repeats", "1",
            "--hash-seeds", "",
            "--orders", "0",
            "--isolated", "0",
            "--no-report",
        ]
        if engine == "python":
            env_py = {**os.environ, "FLAKY_PURE_PYTHON": "1", "PYTHONPATH": str(ROOT / "src")}
            res = subprocess.run(
                [sys.executable, "-m", "flaky_triage.cli", *args],
                env=env_py,
                capture_output=True,
                text=True,
                timeout=120,
            )
        else:
            res = subprocess.run(["bash", str(DETECT), *args], capture_output=True, text=True, timeout=120)
        assert res.returncode == 0, f"{engine}: {res.stderr}"
        with (out_dir / "run_meta.csv").open(encoding="utf-8") as fh:
            meta_rows = list(csv.DictReader(fh))
        tz_values = [r["condition_value"] for r in meta_rows if r["condition"] == "tz"]
        assert tz_values == ["UTC", "Pacific/Kiritimati", "Pacific/Pago_Pago"]


def test_task3_trylast_collection_respects_k_and_m_filters(tmp_path):
    """Task 3: -k and -m filters run before flaky_plugin records FLAKY_COLLECT_FILE."""
    import csv

    proj = tmp_path / "filter_proj"
    (proj / "tests").mkdir(parents=True)
    (proj / "tests" / "test_filter.py").write_text(
        "def test_alpha():\n    assert True\n\n"
        "def test_beta():\n    assert True\n\n"
        "def test_gamma():\n    assert True\n",
        encoding="utf-8",
    )
    for engine in ("python", *(["bash"] if os.name != "nt" else [])):
        out_dir = tmp_path / f"filter_out_{engine}"
        args = [
            "--project", str(proj),
            "--out", str(out_dir),
            "--repeats", "2",
            "--hash-seeds", "",
            "--orders", "0",
            "--timezones", "",
            "--isolated", "0",
            "-k", "test_alpha",
            "--no-report",
        ]
        if engine == "python":
            env_py = {**os.environ, "FLAKY_PURE_PYTHON": "1", "PYTHONPATH": str(ROOT / "src")}
            res = subprocess.run(
                [sys.executable, "-m", "flaky_triage.cli", *args],
                env=env_py,
                capture_output=True,
                text=True,
                timeout=120,
            )
        else:
            res = subprocess.run(["bash", str(DETECT), *args], capture_output=True, text=True, timeout=120)
        assert res.returncode == 0, f"{engine}: {res.stderr}"
        collected = (out_dir / "collected.txt").read_text(encoding="utf-8").splitlines()
        assert collected == ["tests/test_filter.py::test_alpha"]
        with (out_dir / "run_meta.csv").open(encoding="utf-8") as fh:
            meta = list(csv.DictReader(fh))
        assert all(r["tests_expected"] == "1" and r["tests_reported"] == "1" for r in meta)
        with (out_dir / "results.csv").open(encoding="utf-8") as fh:
            results = list(csv.DictReader(fh))
        assert all(r["test_id"] == "tests/test_filter.py::test_alpha" for r in results)
        assert all(r["outcome"] == "passed" for r in results)


def test_task4_strict_xpass_recorded_as_xpassed_and_skipped_by_smart_isolated(tmp_path):
    """Task 4: @pytest.mark.xfail(strict=True) XPASS is recorded as xpassed, excluded from smart isolated,
    and default --isolated-runs is 2 when a real failure occurs."""
    import csv
    from flaky_triage import runner

    proj = tmp_path / "xfail_proj"
    (proj / "tests").mkdir(parents=True)
    (proj / "tests" / "test_xfail.py").write_text(
        "import pytest\n\n"
        "@pytest.mark.xfail(strict=True, reason='strict xpass case')\n"
        "def test_strict_xpass():\n"
        "    assert True\n\n"
        "@pytest.mark.xfail(strict=False, reason='normal xfail case')\n"
        "def test_normal_xfail():\n"
        "    assert False\n\n"
        "def test_normal_pass():\n"
        "    assert True\n",
        encoding="utf-8",
    )
    for engine in ("python", *(["bash"] if os.name != "nt" else [])):
        out_dir = tmp_path / f"xfail_out_{engine}"
        args = [
            "--project", str(proj),
            "--out", str(out_dir),
            "--repeats", "2",
            "--hash-seeds", "",
            "--orders", "0",
            "--timezones", "",
            "--isolated-scope", "smart",
        ]
        if engine == "python":
            env_py = {**os.environ, "FLAKY_PURE_PYTHON": "1", "PYTHONPATH": str(ROOT / "src")}
            res = subprocess.run(
                [sys.executable, "-m", "flaky_triage.cli", *args],
                env=env_py,
                capture_output=True,
                text=True,
                timeout=120,
            )
        else:
            res = subprocess.run(["bash", str(DETECT), *args], capture_output=True, text=True, timeout=120)
        assert res.returncode == 0, f"{engine}: {res.stderr}"
        with (out_dir / "results.csv").open(encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        # No isolated runs should have been triggered because only xpassed/xfailed occurred
        assert [r for r in rows if r["condition"] == "isolated"] == []
        xpass_outcomes = {r["outcome"] for r in rows if r["test_id"].endswith("::test_strict_xpass")}
        xfail_outcomes = {r["outcome"] for r in rows if r["test_id"].endswith("::test_normal_xfail")}
        assert xpass_outcomes == {"xpassed"}
        assert xfail_outcomes == {"xfailed"}

    # Also verify log fallback parser converts FAILED + [XPASS(strict)] to xpassed
    expected = ["tests/test_xfail.py::test_strict_xpass"]
    log_text = (
        "============================= test session starts =============================\n"
        "tests/test_xfail.py::test_strict_xpass FAILED                            [100%]\n"
        "================================== FAILURES ===================================\n"
        "______________________________ test_strict_xpass ______________________________\n"
        "[XPASS(strict)] strict xpass case\n"
        "=========================== short test summary info ===========================\n"
        "FAILED tests/test_xfail.py::test_strict_xpass - [XPASS(strict)] strict xpass case\n"
    )
    outcomes, _ = runner._parse_verbose_log_fallback(log_text, expected)
    assert outcomes["tests/test_xfail.py::test_strict_xpass"] == "xpassed"


def test_task5_project_pointing_to_subdirectory_prints_target_hint(tmp_path):
    """Task 5: Passing a test subdirectory without project config as --project exits 4 with --target Hint."""
    repo = tmp_path / "my_repo"
    sub = repo / "tests" / "unit"
    sub.mkdir(parents=True)
    (repo / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    (sub / "test_unit.py").write_text("def test_one():\n    assert True\n", encoding="utf-8")

    expected_hint = (
        "Hint: If you want to diagnose a specific subdirectory or test file, "
        "keep --project pointed at the repository root and pass the subdirectory via --target <path>."
    )
    for engine, res in _run_both_engines(
        [
            "--project", str(sub),
            "--out", str(tmp_path / "sub_out"),
            "--no-report",
        ]
    ):
        assert res.returncode == 4, f"{engine} returned {res.returncode}: {res.stderr}"
        assert expected_hint in res.stderr


def test_task8_default_isolated_runs_is_two(tmp_path):
    """Task 8.1: Default --isolated-runs is 2 when not explicitly specified."""
    import csv

    proj = tmp_path / "iso_default_proj"
    (proj / "tests").mkdir(parents=True)
    (proj / "tests" / "test_fail.py").write_text(
        "def test_always_fails():\n    assert False\n",
        encoding="utf-8",
    )
    for engine in ("python", *(["bash"] if os.name != "nt" else [])):
        out_dir = tmp_path / f"iso_def_{engine}"
        args = [
            "--project", str(proj),
            "--out", str(out_dir),
            "--repeats", "1",
            "--hash-seeds", "",
            "--orders", "0",
            "--timezones", "",
            "--no-report",
        ]
        if engine == "python":
            env_py = {**os.environ, "FLAKY_PURE_PYTHON": "1", "PYTHONPATH": str(ROOT / "src")}
            res = subprocess.run(
                [sys.executable, "-m", "flaky_triage.cli", *args],
                env=env_py,
                capture_output=True,
                text=True,
                timeout=120,
            )
        else:
            res = subprocess.run(["bash", str(DETECT), *args], capture_output=True, text=True, timeout=120)
        assert res.returncode == 0, f"{engine}: {res.stderr}"
        with (out_dir / "results.csv").open(encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        iso_rows = [r for r in rows if r["condition"] == "isolated"]
        assert len(iso_rows) == 2


def test_venv_bin_dir_prepended_to_subprocess_path_both_engines(tmp_path):
    """Task 4: When target project has .venv/bin/python, .venv/bin is prepended to subprocess PATH
    so tests invoking project CLI entrypoints via subprocess.run(['<cli>', ...]) succeed out-of-the-box."""
    import csv
    import stat
    from flaky_triage import runner

    proj = tmp_path / "cli_venv_proj"
    venv_bin = proj / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (proj / "tests").mkdir(parents=True)

    py_link = venv_bin / "python"
    try:
        py_link.symlink_to(sys.executable)
    except OSError:
        import shutil
        shutil.copy2(sys.executable, py_link)

    # Unit check on _prepend_python_bin_to_path preserving .venv/bin without resolving symlink
    patched_path = runner._prepend_python_bin_to_path(str(py_link), "/usr/bin:/bin")
    assert patched_path.split(os.pathsep)[0] == os.path.abspath(str(venv_bin))

    if os.name != "nt":
        cli_script = venv_bin / "lens_test_cli"
        cli_script.write_text("#!/bin/sh\necho 'lens-cli-ready'\n", encoding="utf-8")
        cli_script.chmod(cli_script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

        (proj / "tests" / "test_cli_call.py").write_text(
            "import subprocess\n\n"
            "def test_cli_entrypoint_in_path():\n"
            "    res = subprocess.run(['lens_test_cli', '--help'], capture_output=True, text=True, check=False)\n"
            "    assert res.returncode == 0\n"
            "    assert 'lens-cli-ready' in res.stdout\n",
            encoding="utf-8",
        )

        for engine in ("python", "bash"):
            out_dir = tmp_path / f"venv_path_out_{engine}"
            args = [
                "--project", str(proj),
                "--out", str(out_dir),
                "--repeats", "1",
                "--hash-seeds", "",
                "--orders", "0",
                "--timezones", "",
                "--isolated", "0",
                "--no-report",
            ]
            if engine == "python":
                env_py = {**os.environ, "FLAKY_PURE_PYTHON": "1", "PYTHONPATH": str(ROOT / "src")}
                res = subprocess.run(
                    [sys.executable, "-m", "flaky_triage.cli", *args],
                    env=env_py,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
            else:
                res = subprocess.run(["bash", str(DETECT), *args], capture_output=True, text=True, timeout=120)
            assert res.returncode == 0, f"{engine}: {res.stderr}"
            with (out_dir / "results.csv").open(encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
            assert len(rows) == 1
            assert rows[0]["outcome"] == "passed", f"{engine} failed CLI subprocess test: {rows}"






