"""A suspended stage states who suspended it and why — never a silent bypass."""

from __future__ import annotations

from pathlib import Path

from tests.support import gate, latest_result, make_repo, run_cli, run_fixture_pipeline, stage

REASON = "owner: sajan, 2026-11-04 - linter not yet ported to the new layout"

SUSPENDED_LINT = (
    "[stages.lint]\n"
    "enabled = false\n"
    f'disabled_reason = "{REASON}"\n'
    'commands = ["echo should-not-run; exit 9"]\n'
)


def test_disabled_stage_skips_with_the_recorded_reason(tmp_path: Path) -> None:
    root = make_repo(tmp_path, sections=(SUSPENDED_LINT,))
    result, _context = run_fixture_pipeline(root, command="run", stages=["lint"])

    # A suspension is not a failure: the run passes, and the reason is carried
    # in both the stage result and the gate rather than inferred from a bare
    # `enabled = false`.
    assert result.exit_code == 0
    lint = stage(result, "lint")
    assert lint.status == "SKIP"
    assert lint.skipped_reason == f"disabled: {REASON}"
    disabled = gate(result, "lint.disabled")
    assert REASON in disabled.summary
    assert disabled.details["disabled_reason"] == REASON
    assert disabled.details["enabled"] is False
    # The suspended command must not have run.
    assert all(item.command != "echo should-not-run; exit 9" for item in lint.gates)

    document = latest_result(root)
    entry = next(item for item in document["stages"] if item["name"] == "lint")
    assert entry["skipped_reason"] == f"disabled: {REASON}"
    summary_path = root / document["run"]["paths"]["reports_dir"] / "summary.md"
    summary = summary_path.read_text(encoding="utf-8")
    assert "## Suspended stages" in summary
    assert REASON in summary


def test_disabled_stage_without_a_reason_is_a_configuration_error(tmp_path: Path) -> None:
    root = make_repo(tmp_path, sections=("[stages.lint]\nenabled = false\n",))
    completed = run_cli(root, "validate")

    assert completed.returncode == 2
    assert "does not say why" in completed.stderr
    assert "disabled_reason" in completed.stderr
