"""Packaging, artifact validation and release bundle lifecycle."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ci_fabric.errors import CiError
from ci_fabric.pipeline import ReleaseRequest, run_pipeline
from ci_fabric.release import create_release, list_releases, resolve_release
from tests.support import gate, latest_result, make_repo, run_fixture_pipeline, stage

PACKAGE_SECTIONS = (
    "[stages.package]\n"
    "required = true\n"
    'strategy = "archive"\n'
    'inputs = ["app"]\n'
    'strip_prefix = "app"\n'
    'output = "{artifacts}/package.tar.gz"',
    "[stages.artifact]\n"
    "required = true\n"
    "verify_archives = true\n"
    "scan_secrets = true\n"
    'must_contain = ["index.html"]',
)


def make_packaged(tmp_path: Path, *, release: str = 'changelog = "RELEASE_NOTES.md"') -> Path:
    files: dict[str, str] = {
        "app/index.html": "<html><body>v1</body></html>\n",
        "RELEASE_NOTES.md": "# Notes\n",
    }
    return make_repo(
        tmp_path,
        files=files,
        sections=PACKAGE_SECTIONS,
        release=release,
    )


def test_package_and_artifact_pass_and_are_deterministic(tmp_path: Path) -> None:
    root = make_packaged(tmp_path)
    first, ctx1 = run_fixture_pipeline(root, command="package")
    second, ctx2 = run_fixture_pipeline(root, command="package")
    assert first.exit_code == 0 and second.exit_code == 0
    assert stage(first, "package").status == "PASS"
    assert stage(first, "artifact").status == "PASS"
    # The same committed tree must byte-identically reproduce the archive.
    artifact1 = ctx1.paths.artifacts_dir / "package.tar.gz"
    artifact2 = ctx2.paths.artifacts_dir / "package.tar.gz"
    assert artifact1.is_file() and artifact2.is_file()
    assert artifact1.read_bytes() == artifact2.read_bytes()
    manifest = json.loads(
        (ctx2.paths.artifacts_dir / "package-manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["kind"] == "package"
    assert manifest["artifacts"][0]["sha256"]
    assert (ctx2.paths.artifacts_dir / "SHA256SUMS").is_file()
    assert (ctx2.paths.artifacts_dir / "sbom.cdx.json").is_file()
    assert (ctx2.paths.artifacts_dir / "provenance.json").is_file()


def test_artifact_stage_validates_contents_and_integrity(tmp_path: Path) -> None:
    root = make_packaged(tmp_path)
    result, _context = run_fixture_pipeline(root, command="package")
    assert gate(result, "artifact.contents").status == "PASS"
    assert gate(result, "artifact.secrets").status == "PASS"
    assert "required path(s) present" in gate(result, "artifact.contents").summary


def test_artifact_detects_tampering_after_packaging(tmp_path: Path) -> None:
    root = make_packaged(tmp_path)
    _result, context = run_fixture_pipeline(root, command="package")
    artifact = context.paths.artifacts_dir / "package.tar.gz"
    artifact.write_bytes(artifact.read_bytes() + b"tampered")

    result = run_pipeline(context, command="artifact", stages=["artifact"])
    integrity = gate(result, "artifact.integrity")
    assert integrity.status == "FAIL"
    assert integrity.blocking_failure is True
    assert "package.tar.gz" in integrity.summary
    assert result.exit_code == 1


def test_required_artifact_without_manifest_fails(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        sections=(
            "[stages.artifact]\nrequired = true\nverify_archives = true\nscan_secrets = true",
        ),
    )
    result, _context = run_fixture_pipeline(root, command="artifact")
    manifest_gate = gate(result, "artifact.manifest")
    assert manifest_gate.status == "FAIL"
    assert "no build or package manifest" in manifest_gate.summary
    assert manifest_gate.remediation and "false pass" in manifest_gate.remediation


def test_release_refuses_dirty_worktree(tmp_path: Path) -> None:
    from ci_fabric.context import create_context

    root = make_packaged(tmp_path)
    _result, context = run_fixture_pipeline(root, command="package")
    (root / "app" / "index.html").write_text("<html>dirty</html>\n", encoding="utf-8")
    # The CLI always builds a fresh context per run; do the same here so the
    # dirty state is observed.
    fresh = create_context(context.root, context.config)
    with pytest.raises(CiError, match="dirty"):
        create_release(fresh)


def test_release_bundle_is_created_verified_and_protected(tmp_path: Path) -> None:
    root = make_packaged(tmp_path)
    _result, context = run_fixture_pipeline(root, command="package")
    record = create_release(context)

    assert record.version == "1.0.0"
    bundle_dir = root / ".localci" / "releases" / "1.0.0"
    for name in (
        "release.json",
        "SHA256SUMS",
        "artifact-manifest.json",
        "RELEASE_NOTES.md",
        "package.tar.gz",
    ):
        assert (bundle_dir / name).is_file(), name
    release_json = json.loads((bundle_dir / "release.json").read_text(encoding="utf-8"))
    assert release_json["version"] == "1.0.0"
    assert release_json["revision"] and not release_json["dirty"]
    assert release_json["signing"] == "none (no sign_command configured)"
    # The attestation label states exactly what the bundle proves: provenance
    # is checksummed, but with no signer nothing vouches for its authorship.
    assert release_json["attestation"] == (
        "provenance.json (listed in SHA256SUMS; no signer configured)"
    )
    sums = (bundle_dir / "SHA256SUMS").read_text(encoding="utf-8")
    assert "provenance.json" in sums
    assert "sbom.cdx.json" in sums
    assert list_releases(root, context.config) == ["1.0.0"]
    bundle = resolve_release(root, context.config, "1.0.0")
    assert bundle.version == "1.0.0"
    pointer = json.loads(
        (root / ".localci" / "releases" / "latest.json").read_text(encoding="utf-8")
    )
    assert pointer["version"] == "1.0.0"

    # The same version cannot be silently replaced.
    with pytest.raises(CiError, match="already exists"):
        create_release(context)
    # An explicit --force replaces the bundle in place.
    forced = create_release(context, force=True)
    assert forced.version == "1.0.0"
    history = (root / ".localci" / "releases" / "history.jsonl").read_text(encoding="utf-8")
    assert history.count("\n") == 2


def test_release_tag_is_created_and_is_immutable(tmp_path: Path) -> None:
    from tests.support import git

    root = make_packaged(tmp_path)
    _result, context = run_fixture_pipeline(root, command="package")
    create_release(context, tag=True)
    assert "v1.0.0" in git(root, "tag", "--list").stdout.split()
    with pytest.raises(CiError, match="tag already exists"):
        create_release(context, tag=True, force=True)


def test_release_handoff_through_pipeline(tmp_path: Path) -> None:
    root = make_packaged(tmp_path)
    result, _context = run_fixture_pipeline(
        root,
        command="release",
        stages=["package"],
        release=ReleaseRequest(),
    )
    assert result.exit_code == 0
    assert result.release is not None
    assert result.release["ok"] is True
    assert result.release["version"] == "1.0.0"
    document = latest_result(root)
    assert document["release"]["ok"] is True
    assert document["status"] == "PASS"
    assert (root / ".localci" / "releases" / "1.0.0" / "release.json").is_file()


def test_release_requires_notes_when_configured(tmp_path: Path) -> None:
    # No notes file exists and the changelog points at a missing path.
    root = make_repo(
        tmp_path,
        files={"app/index.html": "<html></html>\n"},
        sections=PACKAGE_SECTIONS,
        release='require_notes = true\nchangelog = "ABSENT.md"',
    )
    _result, context = run_fixture_pipeline(root, command="package")
    with pytest.raises(CiError, match="release notes are required"):
        create_release(context)
