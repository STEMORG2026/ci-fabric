"""Stateful deploy invariants: system-state gates with blocking semantics.

Invariants are commands that must hold on every deploy (migrations applied,
queues empty, flags present). They differ from smoke_commands in the claim
under test — smoke proves the application answers, invariants prove the
*system state* around it — and share the enforcement: a failed invariant
blocks activation exactly like a failed health check, auto-rollback
included, and `./ci smoke` re-proves them on every invocation.
"""

from __future__ import annotations

from pathlib import Path

from ci_fabric.deployment import deploy, health, smoke, undeploy
from ci_fabric.release import create_release
from tests.support import (
    bump_version,
    commit_all,
    make_repo,
    run_fixture_pipeline,
    write_files,
)

# The invariant reads a state file the server maintains: "migrations applied"
# in miniature. The server's /version endpoint proves the app layer is fine
# while the invariant interrogates the system state around it.
INVARIANT_SECTIONS = [
    '[stages.package]\nrequired = true\nstrategy = "archive"\n'
    'inputs = ["app"]\nstrip_prefix = "app"\noutput = "{artifacts}/package.tar.gz"',
    "[stages.artifact]\nrequired = true\nverify_archives = true\n"
    'scan_secrets = true\nmust_contain = ["server.py"]',
    "[deploy.local]\n"
    'strategy = "process"\n'
    "port = 0\n"
    'health_path = "/health"\n'
    'readiness_path = "/ready"\n'
    'health_expect = "ok"\n'
    'smoke_path = "/version"\n'
    'start_command = "{python} server.py"\n'
    'invariants = ["test -f migrations-applied"]\n'
    "stop_timeout = 5\n"
    "health_timeout = 15\n"
    "smoke_timeout = 30",
]


def make_invariant_repo(tmp_path: Path, *, server: str) -> Path:
    return make_repo(
        tmp_path,
        files={
            "app/server.py": server,
            "app/migrations-applied": "applied\n",
            "RELEASE_NOTES.md": "# Notes\n",
        },
        sections=INVARIANT_SECTIONS,
        release='changelog = "RELEASE_NOTES.md"',
    )


def test_deploy_passes_invariants_and_smoke_rechecks_them(tmp_path: Path) -> None:
    from tests.support import process_server_source

    root = make_invariant_repo(tmp_path, server=process_server_source(version="v1.0.0"))
    _result, context = run_fixture_pipeline(root, command="ci")
    create_release(context)
    try:
        outcome = deploy(context, "local")
        assert outcome.ok, outcome.error
        invariant = next(c for c in outcome.checks if c.name.startswith("invariant"))
        assert invariant.ok
        assert "migration" not in invariant.detail  # it is just an exit-0 command
        # The standing contract: ./ci smoke re-proves the system state.
        recheck = smoke(context, "local")
        assert recheck.ok, recheck.error
        invariant_again = next(c for c in recheck.checks if c.name.startswith("invariant"))
        assert invariant_again.ok
    finally:
        undeploy(context, "local")


def test_failed_invariant_blocks_activation_and_rolls_back(tmp_path: Path) -> None:
    from tests.support import process_server_source

    root = make_invariant_repo(tmp_path, server=process_server_source(version="v1.0.0"))
    _result, context = run_fixture_pipeline(root, command="ci")
    create_release(context)
    good = deploy(context, "local")
    assert good.ok, good.error

    # v1.0.1 ships a server that starts healthy but "forgets the migration":
    # the app layer is green, the invariant is not — and the invariant wins.
    bump_version(root, "1.0.1")
    write_files(root, {"app/server.py": process_server_source(version="v1.0.1")})
    (root / "app" / "migrations-applied").unlink()  # the migration marker is gone
    commit_all(root, "release 1.0.1 (migration missing)")
    _result2, context2 = run_fixture_pipeline(root, command="ci")
    create_release(context2)
    try:
        bad = deploy(context2, "local")
        assert not bad.ok
        assert "invariant failed" in (bad.error or "")
        invariant = next(c for c in bad.checks if c.name.startswith("invariant"))
        assert not invariant.ok
        assert bad.rolled_back is True
        assert bad.rollback_version == "1.0.0"
        restored = health(context2, "local")
        assert restored.ok
        assert restored.record.version == "1.0.0"
    finally:
        undeploy(context2, "local")
