"""Stable contracts for the result document and every emitted JSON file.

These tests pin the shapes downstream tooling (report readers, hosted-CI
adapters, dashboards) is allowed to depend on. A change here is a breaking
change and must bump the relevant schema version.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import gate, latest_result, make_repo, run_fixture_pipeline, stage

RESULT_TOP_LEVEL = {
    "schema_version",
    "tool",
    "command",
    "status",
    "exit_code",
    "project",
    "run",
    "environment",
    "policy",
    "counts",
    "stages",
    "blocking_failures",
    "artifacts",
    "release",
    "error",
    "metadata",
}
RUN_KEYS = {"id", "started_at", "finished_at", "duration_seconds", "git", "paths"}
GIT_KEYS = {
    "available",
    "root",
    "branch",
    "revision",
    "short_revision",
    "dirty",
    "dirty_files",
    "commit_epoch",
    "tags_at_head",
    "tracked_files",
}
COUNT_KEYS = {
    "stages",
    "gates",
    "pass",
    "fail",
    "warn",
    "skip",
    "error",
    "blocking_failures",
    "warning_gates",
}
STAGE_KEYS = {"name", "status", "duration_seconds", "skipped_reason", "error", "gates"}
GATE_KEYS = {
    "id",
    "stage",
    "title",
    "status",
    "classification",
    "summary",
    "command",
    "exit_code",
    "duration_seconds",
    "log",
    "remediation",
    "details",
    "artifacts",
}
ENVIRONMENT_KEYS = {
    "run_id",
    "started_at",
    "source_date_epoch",
    "profile",
    "clean_env",
    "platform",
    "environment",
    "tools",
    "git",
    "config_path",
    "paths",
}
GATE_STATUSES = {"PASS", "FAIL", "WARN", "SKIP", "ERROR"}
CLASSIFICATIONS = {"BLOCKING", "WARNING", "INFO"}


def _assert_document_shape(document: dict) -> None:
    assert set(document) == RESULT_TOP_LEVEL
    assert document["schema_version"] == 1
    assert document["tool"]["name"] == "ci-fabric"
    assert document["tool"]["version"]
    assert isinstance(document["exit_code"], int)
    assert isinstance(document["stages"], list)
    assert set(document["run"]) == RUN_KEYS
    assert set(document["run"]["git"]) == GIT_KEYS
    assert set(document["counts"]) == COUNT_KEYS
    assert set(document["environment"]) == ENVIRONMENT_KEYS
    for stage_doc in document["stages"]:
        assert set(stage_doc) == STAGE_KEYS
        assert stage_doc["status"] in {"PASS", "FAIL", "WARN", "SKIP"}
        for gate_doc in stage_doc["gates"]:
            assert set(gate_doc) == GATE_KEYS
            assert gate_doc["status"] in GATE_STATUSES
            assert gate_doc["classification"] in CLASSIFICATIONS
            assert isinstance(gate_doc["duration_seconds"], float)


def test_result_document_contract_on_success(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        sections=('[stages.lint]\nrequired = true\ncommands = ["true"]',),
    )
    result, context = run_fixture_pipeline(root, command="lint")
    assert result.exit_code == 0
    document = latest_result(root)
    _assert_document_shape(document)
    assert document["status"] == "PASS"
    assert document["blocking_failures"] == []
    assert document["error"] is None
    counts = document["counts"]
    gates_total = sum(len(s["gates"]) for s in document["stages"])
    assert counts["gates"] == gates_total
    assert (
        counts["pass"] + counts["fail"] + counts["warn"] + counts["skip"] + counts["error"]
        == gates_total
    )
    assert counts["blocking_failures"] == 0
    # The on-disk result and the latest pointer are identical documents.
    result_path = root / document["run"]["paths"]["result_path"]
    assert json.loads(result_path.read_text(encoding="utf-8")) == document
    # Latest result round-trips through report.latest_result.
    from ci_fabric.report import latest_result as read_latest

    assert read_latest(context) == document


def test_result_document_contract_on_failure(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        sections=('[stages.lint]\nrequired = true\ncommands = ["echo boom; exit 9"]',),
    )
    result, _context = run_fixture_pipeline(root, command="lint")
    assert result.exit_code == 1
    document = latest_result(root)
    _assert_document_shape(document)
    assert document["status"] == "FAIL"
    failure = document["blocking_failures"][0]
    assert failure["id"] == "lint.01"
    assert failure["stage"] == "lint"
    assert "exit 9" in failure["summary"]
    assert failure["log"].endswith(".log")
    assert failure["remediation"]
    # The markdown summary names the blocking failure and links the evidence.
    summary_path = (
        root / document["run"]["paths"]["logs_dir"].replace("/logs/", "/reports/") / "summary.md"
    )
    assert summary_path.is_file()
    summary = summary_path.read_text(encoding="utf-8")
    assert "# CI run" in summary
    assert "## Blocking failures" in summary
    assert "`lint.01`" in summary
    assert "- Log: `" in summary
    # The policy snapshot records the config hash it ran with.
    from ci_fabric.util import sha256_file

    policy = json.loads((root / ".ci" / "policy.json").read_text(encoding="utf-8"))
    assert policy["schema_version"] == 1
    assert policy["config_sha256"] == sha256_file(root / "ci.toml")
    assert policy["policy"]["profile"] == "minimal"


def test_stage_and_gate_object_contract(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        sections=('[stages.lint]\nrequired = true\ncommands = ["true"]',),
    )
    result, _context = run_fixture_pipeline(root, command="check")
    lint = stage(result, "lint")
    assert lint.skipped_reason is None
    assert lint.error is None
    assert lint.status == "PASS"
    command_gate = gate(result, "lint.01")
    assert command_gate.classification == "BLOCKING"
    assert command_gate.command == "true"
    assert command_gate.exit_code == 0
    assert command_gate.log and command_gate.log.endswith(".log")
    skipped = stage(result, "format")
    assert skipped.status == "SKIP"
    assert skipped.skipped_reason == "not configured"
    document = latest_result(root)
    skipped_doc = next(s for s in document["stages"] if s["name"] == "format")
    assert skipped_doc["skipped_reason"] == "not configured"
    assert skipped_doc["gates"][0]["status"] == "SKIP"
