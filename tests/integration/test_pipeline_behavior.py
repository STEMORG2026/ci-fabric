"""Pipeline orchestration: order, fail-fast, diagnostics, warnings, idempotence."""

from __future__ import annotations

import subprocess
from pathlib import Path

from tests.support import gate, latest_result, make_repo, run_fixture_pipeline, stage

FAILING_LINT = '[stages.lint]\nrequired = true\ncommands = ["echo lint-problem; exit 4"]'


def test_blocking_failure_reports_full_diagnostics(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        sections=(
            '[stages.format]\nrequired = true\ncommands = ["echo format-problem; exit 3"]',
            FAILING_LINT,
        ),
    )
    result, _context = run_fixture_pipeline(root, command="run", stages=["format", "lint"])

    assert result.status == "FAIL"
    assert result.exit_code == 1
    fmt = stage(result, "format")
    assert fmt.status == "FAIL"
    failed_gate = gate(result, "format.01")
    assert failed_gate.command == "echo format-problem; exit 3"
    assert failed_gate.exit_code == 3
    assert failed_gate.classification == "BLOCKING"
    # A blocking failure must always carry a remediation hint.
    assert failed_gate.remediation
    assert failed_gate.log and (root / failed_gate.log).is_file()
    assert "format-problem" in (root / failed_gate.log).read_text(encoding="utf-8")

    document = latest_result(root)
    assert document["status"] == "FAIL"
    assert document["exit_code"] == 1
    failures = document["blocking_failures"]
    assert [item["id"] for item in failures] == ["format.01"]
    assert failures[0]["stage"] == "format"
    assert failures[0]["summary"].startswith("exit 3")
    assert failures[0]["log"]
    assert failures[0]["remediation"]
    assert document["metadata"]["selected_stages"] == ["precheck", "format", "lint"]


def test_fail_fast_skips_later_stages_with_reason(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        sections=(
            '[stages.format]\nrequired = true\ncommands = ["false"]',
            FAILING_LINT,
        ),
    )
    result, _context = run_fixture_pipeline(root, command="run", stages=["format", "lint"])
    lint = stage(result, "lint")
    assert lint.status == "SKIP"
    assert lint.skipped_reason == "fail-fast after a blocking failure"
    assert lint.gates == []
    assert gate(result, "format.01").blocking_failure is True


def test_fail_fast_disabled_runs_remaining_stages(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        sections=(
            '[stages.format]\nrequired = true\ncommands = ["false"]',
            FAILING_LINT,
        ),
        policy="fail_fast = false",
    )
    result, _context = run_fixture_pipeline(root, command="run", stages=["format", "lint"])
    assert stage(result, "format").status == "FAIL"
    # With fail_fast disabled the second stage still executes (and also fails).
    lint = stage(result, "lint")
    assert lint.status == "FAIL"
    assert gate(result, "lint.01").exit_code == 4
    assert result.exit_code == 1


def test_non_required_stage_failure_is_a_warning_not_a_failure(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        sections=('[stages.lint]\nrequired = false\ncommands = ["echo advisory; exit 2"]',),
    )
    result, _context = run_fixture_pipeline(root, command="lint")
    assert result.status == "WARN"
    assert result.exit_code == 0
    lint = stage(result, "lint")
    assert lint.status == "WARN"
    advisory = gate(result, "lint.01")
    assert advisory.status == "FAIL"
    assert advisory.classification == "WARNING"
    assert advisory.blocking_failure is False
    document = latest_result(root)
    assert document["counts"]["warning_gates"] == 1
    assert document["blocking_failures"] == []


def test_format_fix_mode_repairs_and_then_passes(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        files={"bad.py": "x=1\n"},
        sections=(
            "[stages.format]\n"
            "required = true\n"
            "commands = [\"grep -q 'x = 1' bad.py\"]\n"
            "fix_commands = [\"sed -i 's/x=1/x = 1/' bad.py\"]",
        ),
    )
    before, _context = run_fixture_pipeline(root, command="format")
    assert before.status == "FAIL"
    assert gate(before, "format.01").exit_code == 1

    after, _context = run_fixture_pipeline(root, command="format", fix=True)
    assert after.status == "PASS"
    assert (root / "bad.py").read_text(encoding="utf-8") == "x = 1\n"

    check_again, _context = run_fixture_pipeline(root, command="format")
    assert check_again.status == "PASS"


def test_repeated_runs_are_idempotent_and_leave_clean_tree(tmp_path: Path) -> None:
    root = make_repo(tmp_path, sections=('[stages.lint]\nrequired = true\ncommands = ["true"]',))
    first, _context = run_fixture_pipeline(root, command="lint")
    second, _context = run_fixture_pipeline(root, command="lint")
    assert first.exit_code == 0
    assert second.exit_code == 0
    assert second.status == "PASS"
    document = latest_result(root)
    assert document["command"] == "lint"
    assert document["status"] == "PASS"
    results = [p for p in (root / ".ci" / "results").glob("*.json") if p.name != "latest.json"]
    assert len(results) == 2
    status = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert status.stdout.strip() == ""


def test_retention_keeps_only_configured_runs(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        policy="keep_runs = 2",
        sections=('[stages.lint]\nrequired = true\ncommands = ["true"]',),
    )
    for _ in range(3):
        run_fixture_pipeline(root, command="lint")
    results = [p for p in (root / ".ci" / "results").glob("*.json") if p.name != "latest.json"]
    assert len(results) == 2
    log_dirs = [d for d in (root / ".ci" / "logs").iterdir() if d.is_dir()]
    assert sorted(d.name for d in log_dirs) == sorted(p.stem for p in results)


def test_unknown_stage_selection_is_rejected(tmp_path: Path) -> None:
    from ci_fabric.errors import CiError

    root = make_repo(tmp_path)
    try:
        run_fixture_pipeline(root, command="run", stages=["format", "bogus"])
    except CiError as exc:
        assert "unknown stage" in str(exc)
        assert "bogus" in str(exc)
    else:
        raise AssertionError("expected CiError for unknown stage")
