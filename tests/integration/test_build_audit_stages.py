"""Build, package hand-off and audit stages."""

from __future__ import annotations

from pathlib import Path

from tests.support import gate, make_repo, run_fixture_pipeline, stage


def test_build_command_producing_declared_outputs(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        sections=(
            "[stages.build]\n"
            "required = true\n"
            'commands = ["mkdir -p dist && echo payload > dist/app.bin"]\n'
            'outputs = ["dist"]',
        ),
    )
    result, context = run_fixture_pipeline(root, command="build")
    build = stage(result, "build")
    assert build.status == "PASS", [g.summary for g in result.all_gates]
    assert gate(result, "build.01").exit_code == 0
    outputs = gate(result, "build.outputs")
    assert outputs.status == "PASS"
    manifest = (context.paths.artifacts_dir / "build-manifest.json").is_file()
    assert manifest is True


def test_build_missing_outputs_fail_the_stage(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        sections=('[stages.build]\nrequired = true\ncommands = ["true"]\noutputs = ["dist"]',),
    )
    result, _context = run_fixture_pipeline(root, command="build")
    outputs = gate(result, "build.outputs")
    assert outputs.status == "FAIL"
    assert "no files matched outputs" in outputs.summary
    assert "cannot be validated, released, or deployed" in (outputs.remediation or "")
    assert stage(result, "build").status == "FAIL"


def test_failing_build_command_fails_fast_before_outputs(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        sections=(
            "[stages.build]\n"
            "required = true\n"
            'commands = ["echo build-boom; exit 7"]\n'
            'outputs = ["dist"]',
        ),
    )
    result, _context = run_fixture_pipeline(root, command="build")
    command_gate = gate(result, "build.01")
    assert command_gate.status == "FAIL"
    assert command_gate.exit_code == 7
    assert "build-boom" in command_gate.summary
    # The outputs gate must not run after a failed command.
    assert all(g.id != "build.outputs" for g in result.all_gates)


def test_audit_stage_runs_configured_command(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        sections=('[stages.audit]\nrequired = true\ncommands = ["echo audited; true"]',),
    )
    result, _context = run_fixture_pipeline(root, command="audit")
    audit = stage(result, "audit")
    assert audit.status == "PASS"
    assert gate(result, "audit.dependencies.01").exit_code == 0


def test_audit_failure_blocks(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        sections=('[stages.audit]\nrequired = true\ncommands = ["echo CVE-1234; exit 1"]',),
    )
    result, _context = run_fixture_pipeline(root, command="audit")
    audit_gate = gate(result, "audit.dependencies.01")
    assert audit_gate.status == "FAIL"
    assert "CVE-1234" in audit_gate.summary
    assert result.exit_code == 1
