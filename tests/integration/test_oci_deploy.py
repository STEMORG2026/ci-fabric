"""The oci deploy strategy: build from the verified bundle, prove it, hand it off."""

from __future__ import annotations

import json
from pathlib import Path

from ci_fabric.deployment import (
    deploy,
    deploy_summary,
    health,
    rollback,
    smoke,
    status,
    undeploy,
)
from ci_fabric.release import create_release
from tests.support import (
    bump_version,
    commit_all,
    make_repo,
    oci_deploy_sections,
    run_cli,
    run_fixture_pipeline,
    write_files,
)

HANDOFF = """\
import sys

tag = sys.argv[1]
if tag.endswith("1.1.0"):
    print(f"runtime refuses {tag}")
    sys.exit(3)
print(f"loaded {tag}")
"""


def make_oci_repo(
    tmp_path: Path, *, sections: list[str], files: dict[str, str] | None = None
) -> Path:
    all_files = {"RELEASE_NOTES.md": "# Notes\n", "app/index.html": "<html>v1</html>\n"}
    all_files.update(files or {})
    return make_repo(
        tmp_path,
        files=all_files,
        sections=sections,
        release='changelog = "RELEASE_NOTES.md"',
    )


def test_oci_deploy_builds_verifies_and_reports_an_image(tmp_path: Path) -> None:
    root = make_oci_repo(tmp_path, sections=oci_deploy_sections())
    _, context = run_fixture_pipeline(root, command="ci")
    create_release(context)
    record = None
    try:
        outcome = deploy(context, "local")
        assert outcome.ok, outcome.error
        record = outcome.record
        assert record is not None
        assert record.strategy == "oci"
        assert record.status == "active"
        # No process and no invented URL: the image reference is the identity.
        assert record.pid is None
        assert record.base_url == ""
        assert record.image_tag == "fixture-1.0.0"
        assert record.image_digest.startswith("sha256:")
        names = [check.name for check in outcome.checks]
        assert names.index("image.build") < names.index("image.verify")
        assert names.index("image.verify") < names.index("deployment.active")
        assert all(check.ok for check in outcome.checks)

        # The layout and its runtime-loadable archive exist and were recorded.
        assert (root / record.image_dir / "oci-layout").is_file()
        assert (root / record.image_dir / "index.json").is_file()
        assert (root / record.image_archive).is_file()

        # Health is the digest chain, re-proved from disk.
        assert health(context, "local").ok
        assert smoke(context, "local").ok
        state = status(context, "local")
        assert state["running"] is True
        assert state["image"]["ok"] is True
    finally:
        undeploy(context, "local")

    assert record is not None
    current = json.loads(
        (root / ".localci" / "deploy" / "local" / "current.json").read_text(encoding="utf-8")
    )
    assert current["image_digest"] == record.image_digest
    assert current["image_tag"] == "fixture-1.0.0"


def test_a_tampered_layout_fails_health_and_smoke(tmp_path: Path) -> None:
    root = make_oci_repo(tmp_path, sections=oci_deploy_sections())
    _, context = run_fixture_pipeline(root, command="ci")
    create_release(context)
    try:
        outcome = deploy(context, "local")
        assert outcome.ok, outcome.error
        record = outcome.record
        assert record is not None
        manifest = root / record.image_dir / "blobs" / "sha256" / record.image_digest.split(":")[1]
        # Corrupt the manifest blob: the chain from index.json must notice.
        manifest.write_bytes(manifest.read_bytes() + b" ")

        unhealthy = health(context, "local")
        assert not unhealthy.ok
        assert "hashes to" in (unhealthy.error or "")
        assert not smoke(context, "local").ok
        assert status(context, "local")["image"]["ok"] is False
    finally:
        undeploy(context, "local")


def test_image_command_receives_the_immutable_reference(tmp_path: Path) -> None:
    handoff = "sh -c 'echo {image} > handoff.txt'"
    root = make_oci_repo(tmp_path, sections=oci_deploy_sections(image_command=handoff))
    _, context = run_fixture_pipeline(root, command="ci")
    create_release(context)
    try:
        outcome = deploy(context, "local")
        assert outcome.ok, outcome.error
        record = outcome.record
        assert record is not None
        command_check = next(check for check in outcome.checks if check.name == "image.command")
        assert command_check.ok
        # The handoff runs at the repository root, like every other configured command.
        handoff_file = root / "handoff.txt"
        assert handoff_file.read_text(encoding="utf-8").strip() == (
            f"{record.image_tag}@{record.image_digest}"
        )
    finally:
        undeploy(context, "local")


def test_rollback_restores_the_previous_image_by_proof(tmp_path: Path) -> None:
    root = make_oci_repo(tmp_path, sections=oci_deploy_sections())
    _, context = run_fixture_pipeline(root, command="ci")
    create_release(context)
    try:
        first = deploy(context, "local")
        assert first.ok, first.error
        first_record = first.record
        assert first_record is not None

        bump_version(root, "1.1.0", commit=False)
        write_files(root, {"app/index.html": "<html>v2</html>\n"})
        commit_all(root, "release 1.1.0")
        _, context2 = run_fixture_pipeline(root, command="ci")
        create_release(context2)
        second = deploy(context2, "local")
        assert second.ok, second.error
        assert second.record is not None
        assert second.record.image_digest != first_record.image_digest

        rolled = rollback(context2, "local")
        assert rolled.ok, rolled.error
        assert rolled.record is not None and rolled.record.version == "1.0.0"
        assert [check.name for check in rolled.checks] == ["rollback.image.verify"]
        assert rolled.checks[0].ok
        assert health(context2, "local").ok

        # A layout that no longer matches its digest is never reactivated.
        # The next rollback targets 1.1.0, so that is the layout to corrupt.
        assert second.record is not None
        layer = (
            root
            / second.record.image_dir
            / "blobs"
            / "sha256"
            / second.record.image_digest.split(":")[1]
        )
        layer.write_bytes(layer.read_bytes() + b" ")
        refused = rollback(context2, "local")
        assert not refused.ok
        assert "failed verification" in (refused.error or "")
    finally:
        undeploy(context, "local")


def test_status_commands_name_the_image_instead_of_an_empty_url(tmp_path: Path) -> None:
    root = make_oci_repo(tmp_path, sections=oci_deploy_sections())
    _, context = run_fixture_pipeline(root, command="ci")
    create_release(context)
    try:
        outcome = deploy(context, "local")
        assert outcome.ok, outcome.error
        record = outcome.record
        assert record is not None
        reference = f"{record.image_tag}@{record.image_digest}"

        summaries = deploy_summary(context)
        assert summaries[0]["strategy"] == "oci"
        assert summaries[0]["target"] == reference

        listed = run_cli(root, "status")
        assert listed.returncode == 0, listed.stderr
        assert reference in listed.stdout
        assert "(oci, running)" in listed.stdout
        assert "None" not in listed.stdout

        single = run_cli(root, "deploy-status", "--env", "local")
        assert single.returncode == 0, single.stderr
        assert reference in single.stdout
    finally:
        undeploy(context, "local")


def test_a_failing_image_command_blocks_and_rolls_back(tmp_path: Path) -> None:
    root = make_oci_repo(
        tmp_path,
        sections=oci_deploy_sections(image_command="{python} handoff.py {image_tag}"),
        files={"handoff.py": HANDOFF},
    )
    _, context = run_fixture_pipeline(root, command="ci")
    create_release(context)
    try:
        first = deploy(context, "local")
        assert first.ok, first.error

        # 1.1.0's runtime handoff refuses the image: activation must fail and
        # the previous image deployment must be restored by proof, not rebuild.
        bump_version(root, "1.1.0", commit=False)
        write_files(root, {"app/index.html": "<html>v2</html>\n"})
        commit_all(root, "release 1.1.0 with a refusing runtime")
        _, context2 = run_fixture_pipeline(root, command="ci")
        create_release(context2)

        second = deploy(context2, "local")
        assert not second.ok
        assert "image_command failed" in (second.error or "")
        assert second.rolled_back is True
        assert second.rollback_version == "1.0.0"
        restored = health(context2, "local")
        assert restored.ok
        assert restored.record is not None and restored.record.version == "1.0.0"

        failure = json.loads(
            (root / ".localci" / "deploy" / "local" / "last-failure.json").read_text(
                encoding="utf-8"
            )
        )
        assert failure["version"] == "1.1.0"
        history = (
            (root / ".localci" / "deploy" / "local" / "history.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        assert any('"ok": false' in line and "rolled_back" in line for line in history)
    finally:
        undeploy(context, "local")
