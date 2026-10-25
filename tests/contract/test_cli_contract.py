"""Command-line contracts: exit codes, JSON documents, and the ./ci surface.

Every assertion here runs the real CLI in a subprocess, exactly the way a
consumer or a future hosted-CI adapter would invoke it.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import install_shim, make_repo, run_cli

PIPELINE_STAGES = {
    "precheck",
    "repository",
    "format",
    "lint",
    "typecheck",
    "unit",
    "integration",
    "contract",
    "e2e",
    "coverage",
    "security",
    "audit",
    "build",
    "package",
    "artifact",
}


def test_exit_code_zero_on_success(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        sections=('[stages.lint]\nrequired = true\ncommands = ["true"]',),
    )
    completed = run_cli(root, "lint")
    assert completed.returncode == 0, completed.stderr
    assert "RESULT PASS" in completed.stdout


def test_exit_code_one_on_blocking_gate_failure(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        sections=('[stages.lint]\nrequired = true\ncommands = ["echo boom; exit 9"]',),
    )
    completed = run_cli(root, "lint")
    assert completed.returncode == 1
    assert "RESULT FAIL" in completed.stdout
    assert "lint.01" in completed.stdout
    assert "boom" in completed.stdout
    # The failure carries a pointer to its log and a remediation hint.
    assert "log:" in completed.stdout
    assert "fix:" in completed.stdout


def test_exit_code_two_on_config_error_and_missing_config(tmp_path: Path) -> None:
    broken = make_repo(
        tmp_path,
        name="broken",
        config='version = 1\n[policy]\nprofile = "bogus"\n',
    )
    completed = run_cli(broken, "lint")
    assert completed.returncode == 2
    assert "error:" in completed.stderr
    assert "fix:" in completed.stderr

    empty = tmp_path / "empty"
    empty.mkdir()
    missing = run_cli(empty, "lint")
    assert missing.returncode == 2
    assert "error:" in missing.stderr


def test_policy_json_document_contract(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        sections=('[stages.lint]\nrequired = true\ncommands = ["true"]',),
    )
    completed = run_cli(root, "policy", "--json")
    assert completed.returncode == 0
    document = json.loads(completed.stdout)
    assert document["schema_version"] == 1
    assert document["tool"]["name"] == "ci-fabric"
    assert document["config"] == "ci.toml"
    policy = document["policy"]
    assert policy["profile"] == "minimal"
    assert policy["fail_fast"] is True
    assert policy["required_stages"] == ["repository", "security"]
    assert policy["stages"]["lint"]["configured"] is True
    assert policy["stages"]["lint"]["classification"] == "BLOCKING"
    assert policy["stages"]["unit"]["configured"] is False
    pipeline = document["pipeline"]
    assert {entry["stage"] for entry in pipeline} == PIPELINE_STAGES
    for entry in pipeline:
        assert entry["classification"] in {"BLOCKING", "WARNING"}
        assert isinstance(entry["configured"], bool)
        assert isinstance(entry["enabled"], bool)
    repository = next(entry for entry in pipeline if entry["stage"] == "repository")
    assert repository["required"] is True
    assert repository["classification"] == "BLOCKING"


def test_status_json_document_contract(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        sections=('[stages.lint]\nrequired = true\ncommands = ["true"]',),
    )
    fresh = run_cli(root, "status", "--json")
    assert fresh.returncode == 0
    document = json.loads(fresh.stdout)
    assert document["project"]["name"] == "fixture"
    assert document["project"]["version"] == "1.0.0"
    assert document["git"]["dirty"] is False
    assert document["last_run"] is None
    assert document["hooks"]["installed"] is False
    assert document["deployments"] == []
    assert set(document["paths"]) == {
        "result",
        "latest",
        "summary",
        "logs",
        "reports",
        "artifacts",
    }

    assert run_cli(root, "lint").returncode == 0
    after = json.loads(run_cli(root, "status", "--json").stdout)
    assert after["last_run"]["command"] == "lint"
    assert after["last_run"]["status"] == "PASS"


def test_releases_lists_empty_then_created_bundles(tmp_path: Path) -> None:
    from ci_fabric.release import create_release
    from tests.support import run_fixture_pipeline

    root = make_repo(
        tmp_path,
        files={"app/index.html": "<html></html>\n", "RELEASE_NOTES.md": "# Notes\n"},
        sections=(
            '[stages.package]\nrequired = true\nstrategy = "archive"\n'
            'inputs = ["app"]\nstrip_prefix = "app"\noutput = "{artifacts}/package.tar.gz"',
            "[stages.artifact]\nrequired = true\nverify_archives = true\nscan_secrets = true\n"
            'must_contain = ["index.html"]',
        ),
        release='changelog = "RELEASE_NOTES.md"',
    )
    empty = run_cli(root, "releases")
    assert empty.returncode == 0
    assert "no local releases" in empty.stdout
    listing = json.loads(run_cli(root, "releases", "--json").stdout)
    assert listing == []

    _result, context = run_fixture_pipeline(root, command="package")
    create_release(context)
    after = run_cli(root, "releases")
    assert after.returncode == 0
    assert "1.0.0" in after.stdout
    payload = json.loads(run_cli(root, "releases", "--json").stdout)
    assert payload[0]["version"] == "1.0.0"


def test_hooks_cli_contract(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    install_shim(root)

    refused = run_cli(root, "hooks", "install")
    assert refused.returncode == 0, refused.stderr

    status = run_cli(root, "hooks", "status")
    assert status.returncode == 0
    assert "installed: true" in status.stdout

    uninstalled = run_cli(root, "hooks", "uninstall")
    assert uninstalled.returncode == 0
    assert "uninstalled" in uninstalled.stdout

    after = run_cli(root, "hooks", "status")
    assert after.returncode == 1  # not installed is a non-success status
    assert "installed: false" in after.stdout


def test_unknown_command_fails_with_usage(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    completed = run_cli(root, "definitely-not-a-command")
    assert completed.returncode != 0
    assert "invalid choice" in completed.stderr


def test_validate_contract(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    completed = run_cli(root, "validate")
    assert completed.returncode == 0, completed.stderr
    assert "RESULT PASS" in completed.stdout
    document = json.loads(run_cli(root, "status", "--json").stdout)
    assert document["last_run"]["command"] == "validate"
