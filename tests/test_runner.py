"""Argument and failure handling of scripts/detect.sh (exit codes in README.md)."""
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DETECT = ROOT / "scripts" / "detect.sh"
FIXTURES = ROOT / "tests" / "fixtures" / "parser"
GOOD_PROJECT = str(FIXTURES / "outcomes")
OUT = str(ROOT / "out" / "selftest" / "runner")


def detect(*args):
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
    link.symlink_to(external, target_is_directory=True)
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
    link.symlink_to(FIXTURES / "outcomes", target_is_directory=True)
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
                    "--repeats", "08", "--hash-seeds", "", "--orders", "0", "--isolated", "0")
    assert result.returncode == 0, result.stderr
    assert "planned 8 runs" in result.stderr


def test_help_exits_0():
    result = detect("--help")
    assert result.returncode == 0
    assert "Usage: scripts/detect.sh" in result.stdout
