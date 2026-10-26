"""End-to-end flows: scaffold, security lifecycle, release + deploy, shim."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from tests.support import (
    PROJECT_ROOT,
    commit_all,
    install_shim,
    make_repo,
    run_cli,
    static_deploy_sections,
    write_files,
)

AWS_KEY = "AKIA" + "ABCDEFGHIJKLMNOP"


def git_init(root: Path) -> None:
    assert subprocess.run(["git", "init", "-b", "main", str(root)], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "E2E"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "e2e@example.invalid"], check=True
    )
    commit_all(root, "initial project")


def test_init_scaffolds_a_working_repository(tmp_path: Path) -> None:
    root = tmp_path / "project"
    write_files(
        root,
        {
            "README.md": "# Demo\n",
            "LICENSE": "MIT\n",
            "pyproject.toml": (
                '[project]\nname = "demo"\nversion = "0.1.0"\n'
                '[build-system]\nrequires = ["setuptools"]\n'
            ),
            "tests/test_demo.py": "def test_ok():\n    assert True\n",
        },
    )
    git_init(root)

    initialized = run_cli(root, "init")
    assert initialized.returncode == 0, initialized.stderr
    assert (root / "ci.toml").is_file()
    assert (root / "ci").is_file()
    assert os.access(root / "ci", os.X_OK)
    assert ".ci/" in (root / ".gitignore").read_text(encoding="utf-8")

    validated = run_cli(root, "validate")
    assert validated.returncode == 0, validated.stdout + validated.stderr
    tested = run_cli(root, "test")
    assert tested.returncode == 0, tested.stdout[-2000:]
    checked = run_cli(root, "check")
    assert checked.returncode == 0, checked.stdout[-2000:]


def test_security_lifecycle_finds_and_resolves_committed_secret(tmp_path: Path) -> None:
    root = make_repo(tmp_path, files={"leak.py": f"aws_key = '{AWS_KEY}'\n"})
    failed = run_cli(root, "security")
    assert failed.returncode == 1
    assert "security.secrets" in failed.stdout
    assert "1 potential secret(s) in" in failed.stdout
    assert "rotate" in failed.stdout
    assert "log:" in failed.stdout

    (root / "leak.py").unlink()
    commit_all(root, "remove leaked key")
    resolved = run_cli(root, "security")
    assert resolved.returncode == 0, resolved.stdout[-2000:]
    assert "PASS security" in resolved.stdout


def test_failing_run_is_queryable_through_status(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        sections=('[stages.lint]\nrequired = true\ncommands = ["exit 5"]',),
    )
    assert run_cli(root, "lint").returncode == 1
    document = json.loads(run_cli(root, "status", "--json").stdout)
    assert document["last_run"]["command"] == "lint"
    assert document["last_run"]["status"] == "FAIL"
    assert document["last_run"]["exit_code"] == 1


def test_release_and_static_deploy_flow_through_the_cli(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        files={
            "app/index.html": "<html><body>e2e</body></html>\n",
            "RELEASE_NOTES.md": "# Notes\n",
        },
        sections=static_deploy_sections(),
        release='changelog = "RELEASE_NOTES.md"',
    )
    full = run_cli(root, "ci")
    assert full.returncode == 0, full.stdout[-3000:] + full.stderr[-1000:]
    assert "RESULT PASS" in full.stdout

    released = run_cli(root, "release")
    assert released.returncode == 0, released.stdout[-3000:] + released.stderr[-1000:]
    assert "released fixture 1.0.0" in released.stdout

    deployed = run_cli(root, "deploy", "--env", "local")
    assert deployed.returncode == 0, deployed.stdout[-3000:] + deployed.stderr[-1000:]
    assert "DEPLOY OK" in deployed.stdout

    healthy = run_cli(root, "health", "--env", "local")
    assert healthy.returncode == 0
    assert "HEALTH OK" in healthy.stdout

    status_document = json.loads(run_cli(root, "deploy-status", "--env", "local", "--json").stdout)
    assert status_document["deployed"] is True
    assert status_document["running"] is True
    assert status_document["record"]["version"] == "1.0.0"
    assert status_document["health"]["ok"] is True

    try:
        smoke = run_cli(root, "smoke", "--env", "local")
        assert smoke.returncode == 0, smoke.stdout[-2000:]
        assert "SMOKE OK" in smoke.stdout
    finally:
        stopped = run_cli(root, "undeploy", "--env", "local")
        assert stopped.returncode == 0, stopped.stdout[-2000:]
    summary = json.loads(run_cli(root, "deploy-status", "--json").stdout)
    assert summary["environments"][0]["running"] is False


def test_shim_resolves_ci_fabric_src_and_reports_version(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    shim = install_shim(root)
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment["CI_FABRIC_SRC"] = str(PROJECT_ROOT)
    completed = subprocess.run(
        [str(shim), "version"],
        cwd=root,
        env=environment,
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.startswith("ci-fabric ")


def test_docs_command_generates_checks_and_restores(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    reference = root / "docs" / "reference.md"
    assert not reference.exists()

    created = run_cli(root, "docs")
    assert created.returncode == 0, created.stderr
    assert reference.is_file()
    committed_text = reference.read_text(encoding="utf-8")
    assert "GENERATED" in committed_text

    fresh = run_cli(root, "docs", "--check")
    assert fresh.returncode == 0

    reference.write_text("# tampered\n", encoding="utf-8")
    stale = run_cli(root, "docs", "--check")
    assert stale.returncode == 1
    assert "stale" in stale.stdout
    assert "./ci docs" in stale.stdout

    restored = run_cli(root, "docs")
    assert restored.returncode == 0
    assert reference.read_text(encoding="utf-8") == committed_text
