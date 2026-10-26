"""Three-outcome gate semantics: pass, fail, and unverified.

A missing tool or a timed-out command is the *absence* of a verdict, not a
verdict of fail — "pytest is not installed" must never be recorded as "pytest
found failing tests". Enforcement is unchanged: an unverified gate on a
blocking stage still blocks. The distinction exists so downstream readers of
the result document can tell an unread gate from a red one (the same defect
Universal_Software_Auditor fixed in its own gate records).
"""

from __future__ import annotations

from pathlib import Path

from tests.support import gate, make_repo, run_fixture_pipeline, stage


def test_missing_tool_is_unverified_not_failed(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        sections=(
            '[stages.lint]\nrequired = true\ncommands = ["definitely-not-a-real-tool-ci-fabric"]',
        ),
    )
    result, _context = run_fixture_pipeline(root, command="lint")
    command_gate = gate(result, "lint.01")
    assert command_gate.status == "ERROR"
    assert command_gate.details.get("outcome") == "unverified"
    assert command_gate.exit_code == 127
    # An unread gate still blocks a required stage.
    assert command_gate.blocking_failure is True
    assert result.exit_code == 1
    assert stage(result, "lint").status == "FAIL"
    document_gate = gate(result, "lint.01").to_dict()
    assert document_gate["status"] == "ERROR"
    assert document_gate["details"]["outcome"] == "unverified"


def test_genuine_failure_is_still_fail(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        sections=('[stages.lint]\nrequired = true\ncommands = ["exit 3"]',),
    )
    result, _context = run_fixture_pipeline(root, command="lint")
    command_gate = gate(result, "lint.01")
    assert command_gate.status == "FAIL"
    assert "outcome" not in command_gate.details
    assert command_gate.blocking_failure is True


def test_timeout_is_unverified_and_blocks(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        sections=('[stages.lint]\nrequired = true\ntimeout = 1\ncommands = ["sleep 30"]',),
    )
    result, _context = run_fixture_pipeline(root, command="lint")
    command_gate = gate(result, "lint.01")
    assert command_gate.status == "ERROR"
    assert command_gate.details.get("outcome") == "unverified"
    assert command_gate.exit_code is None or command_gate.exit_code != 0
    assert command_gate.blocking_failure is True
    assert result.exit_code == 1


def test_unverified_test_command_is_error_not_fail(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        files={"tests/test_ok.py": "def test_ok():\n    assert True\n"},
        sections=(
            "[stages.unit]\n"
            "required = true\n"
            'commands = ["not-a-real-test-runner"]\n'
            'test_globs = ["tests/**/*.py"]'
        ),
    )
    result, _context = run_fixture_pipeline(root, command="unit")
    command_gate = gate(result, "unit.01")
    assert command_gate.status == "ERROR"
    assert command_gate.details.get("outcome") == "unverified"
    # A discovery that cannot even run must never pass as a silent zero.
    assert stage(result, "unit").status == "FAIL"
