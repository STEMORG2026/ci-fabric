"""Deployment lifecycle: static and process strategies, rollback, auto-rollback."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ci_fabric.deployment import (
    deploy,
    deploy_summary,
    health,
    rollback,
    smoke,
    undeploy,
)
from ci_fabric.errors import CiError
from ci_fabric.release import create_release
from tests.support import (
    bump_version,
    commit_all,
    make_repo,
    process_deploy_sections,
    process_server_source,
    run_fixture_pipeline,
    static_deploy_sections,
    write_files,
    write_static_app,
)


def make_deployable(tmp_path: Path, *, sections: list[str], files: dict[str, str]) -> Path:
    all_files = {"RELEASE_NOTES.md": "# Notes\n"}
    all_files.update(files)
    return make_repo(
        tmp_path,
        files=all_files,
        sections=sections,
        release='changelog = "RELEASE_NOTES.md"',
    )


def test_static_deploy_health_rollback_and_undeploy(tmp_path: Path) -> None:
    root = make_deployable(
        tmp_path,
        sections=static_deploy_sections(),
        files={"app/index.html": "<html>v1</html>\n"},
    )
    _, context = run_fixture_pipeline(root, command="ci")
    create_release(context)
    try:
        outcome = deploy(context, "local")
        assert outcome.ok, outcome.error
        assert outcome.record.status == "active"
        assert outcome.record.version == "1.0.0"
        assert outcome.record.base_url.startswith("http://127.0.0.1:")
        assert outcome.record.pid is not None
        assert health(context, "local").ok
        smoke_result = smoke(context, "local")
        assert smoke_result.ok, smoke_result.error
        current = json.loads(
            (root / ".localci" / "deploy" / "local" / "current.json").read_text(encoding="utf-8")
        )
        assert current["version"] == "1.0.0"

        # Release and deploy a second version; the first becomes the rollback target.
        bump_version(root, "1.1.0")
        write_static_app(root, "v2")
        commit_all(root, "release 1.1.0")
        _, context2 = run_fixture_pipeline(root, command="ci")
        create_release(context2)
        second = deploy(context2, "local")
        assert second.ok, second.error
        assert second.record.version == "1.1.0"
        previous = json.loads(
            (root / ".localci" / "deploy" / "local" / "previous.json").read_text(encoding="utf-8")
        )
        assert previous["version"] == "1.0.0"

        rolled = rollback(context2, "local")
        assert rolled.ok, rolled.error
        assert rolled.record.version == "1.0.0"
        assert health(context2, "local").ok
        smoke_after = smoke(context2, "local")
        assert smoke_after.ok, smoke_after.error
    finally:
        undeploy(context, "local")
    summary = deploy_summary(context)
    assert summary[0]["environment"] == "local"
    assert summary[0]["running"] is False
    assert summary[0]["status"] == "stopped"


def test_process_deploy_auto_rollback_on_failed_release(tmp_path: Path) -> None:
    root = make_deployable(
        tmp_path,
        sections=process_deploy_sections(smoke_expect="v1.0.0"),
        files={"app/server.py": process_server_source(fail=False, version="v1.0.0")},
    )
    _, context = run_fixture_pipeline(root, command="ci")
    create_release(context)
    try:
        good = deploy(context, "local")
        assert good.ok, good.error
        assert health(context, "local").ok
        assert smoke(context, "local").ok

        # A release whose server process exits during startup must not take the
        # environment down: auto-rollback restores the previous release.
        bump_version(root, "1.0.1")
        write_files(root, {"app/server.py": process_server_source(fail=True, version="v1.0.1")})
        commit_all(root, "release 1.0.1 (broken)")
        _, context2 = run_fixture_pipeline(root, command="ci")
        create_release(context2)
        bad = deploy(context2, "local")
        assert not bad.ok
        assert bad.rolled_back is True
        assert bad.rollback_version == "1.0.0"
        assert "rollback" in (bad.error or "")
        restored = health(context2, "local")
        assert restored.ok
        assert restored.record.version == "1.0.0"
        failure = json.loads(
            (root / ".localci" / "deploy" / "local" / "last-failure.json").read_text(
                encoding="utf-8"
            )
        )
        assert failure["version"] == "1.0.1"
        history = (
            (root / ".localci" / "deploy" / "local" / "history.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        assert any('"ok": false' in line and "rolled_back" in line for line in history)
    finally:
        undeploy(context, "local")


def test_deploy_rejects_unknown_environment(tmp_path: Path) -> None:
    root = make_deployable(
        tmp_path,
        sections=static_deploy_sections(),
        files={"app/index.html": "<html></html>\n"},
    )
    _, context = run_fixture_pipeline(root, command="ci")
    with pytest.raises(CiError, match="unknown deployment environment"):
        deploy(context, "production")
    with pytest.raises(CiError, match="unknown deployment environment"):
        rollback(context, "production")
