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
OUT = str(ROOT / "out" / "selftest" / "runner")


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
        (["--project", GOOD_PROJECT, "--frobnicate"], "unknown option"),
        (["--project"], "needs a value"),
    ],
)
def test_bad_arguments_exit_2(args, message):
    result = detect(*args)
    assert result.returncode == 2
    assert message in result.stderr


def test_missing_pytest_exits_3():
    result = detect("--project", GOOD_PROJECT, "--out", OUT, "--python", "/usr/bin/false")
    assert result.returncode == 3
    assert "pytest is not installed" in result.stderr


@pytest.mark.parametrize("case, message", [
    ("collection_error", "could not collect"),
    ("no_tests", "found no tests"),
])
def test_collection_problems_exit_4(case, message):
    result = detect("--project", str(FIXTURES / case), "--out", OUT, "--no-report")
    assert result.returncode == 4
    assert message in result.stderr


def test_failed_collection_keeps_previous_results():
    out = OUT + "_keep"
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

    link = ROOT / "out" / "selftest" / "external_link"
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.exists() or link.is_symlink():
        link.unlink()
    try:
        link.symlink_to(external, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks not permitted in this environment: {exc}")
    try:
        result = detect("--project", GOOD_PROJECT, "--out", str(link), "--no-report")
    finally:
        link.unlink()

    assert result.returncode == 2
    assert "--out must not be a symlink" in result.stderr
    assert sentinel.read_text() == "must survive runner validation\n"


def test_symlink_alias_of_the_project_is_rejected():
    link = ROOT / "out" / "selftest" / "link_to_project"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.unlink(missing_ok=True)
    try:
        link.symlink_to(FIXTURES / "outcomes", target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks not permitted in this environment: {exc}")
    try:
        result = detect("--project", GOOD_PROJECT, "--out", str(link), "--no-report")
    finally:
        link.unlink()
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


def test_leading_zero_numbers_are_decimal():
    # 08 is not valid octal; Bash arithmetic would reject it without 10#
    result = detect("--project", GOOD_PROJECT, "--out", OUT, "--no-report",
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



