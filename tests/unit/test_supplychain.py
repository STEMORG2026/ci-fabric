"""Deterministic packaging, checksums, SBOM and provenance."""

from __future__ import annotations

import json
import tarfile
import time
import zipfile
from pathlib import Path

import pytest

from ci_fabric.config import load_config
from ci_fabric.context import create_context
from ci_fabric.errors import CiError
from ci_fabric.supplychain import (
    build_provenance,
    build_sbom,
    create_archive,
    find_lockfiles,
    load_manifest,
    verify_archive,
    verify_checksums,
    write_checksums,
    write_manifest,
)
from tests.support import make_repo


def make_tree(root: Path) -> Path:
    content = root / "dist"
    content.mkdir(parents=True)
    (content / "b.txt").write_text("bravo\n", encoding="utf-8")
    (content / "a.txt").write_text("alpha\n", encoding="utf-8")
    (content / "nested").mkdir()
    (content / "nested" / "c.txt").write_text("charlie\n", encoding="utf-8")
    (content / ".DS_Store").write_bytes(b"junk")
    return content


def test_archive_is_deterministic_and_sorted(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    make_tree(root)
    outputs: list[bytes] = []
    for index in range(2):
        # Touching the inputs must not change the archive.
        for path in sorted((root / "dist").rglob("*")):
            if path.is_file():
                time.sleep(0.01)
                path.touch()
        output = tmp_path / f"out-{index}.tar.gz"
        result = create_archive(
            root=root,
            inputs=[root / "dist"],
            output=output,
            fmt="tar.gz",
            epoch=1_700_000_000,
            excludes=[".DS_Store"],
            strip_prefix="dist",
        )
        assert result.entries == ["a.txt", "b.txt", "nested/c.txt"]
        outputs.append(output.read_bytes())
    assert outputs[0] == outputs[1]


def test_zip_archive_deterministic(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    make_tree(root)
    first = tmp_path / "one.zip"
    second = tmp_path / "two.zip"
    for output in (first, second):
        create_archive(
            root=root,
            inputs=[root / "dist"],
            output=output,
            fmt="zip",
            epoch=1_700_000_000,
            excludes=[".DS_Store"],
            strip_prefix="dist",
        )
    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as archive:
        assert archive.namelist() == ["a.txt", "b.txt", "nested/c.txt"]


def test_archive_rejects_duplicates_and_empty_inputs(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    make_tree(root)
    with pytest.raises(CiError, match="no files matched"):
        create_archive(
            root=root,
            inputs=[root / "dist"],
            output=tmp_path / "empty.tar",
            fmt="tar",
            epoch=1_700_000_000,
            excludes=["**/*"],
            strip_prefix="dist",
        )
    with pytest.raises(CiError, match="duplicate archive entry"):
        create_archive(
            root=root,
            inputs=[root / "dist" / "a.txt", root / "dist"],
            output=tmp_path / "dup.tar",
            fmt="tar",
            epoch=1_700_000_000,
            excludes=[],
            strip_prefix="dist",
        )


def test_archive_validates_strip_prefix(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    make_tree(root)
    with pytest.raises(CiError, match="outside strip_prefix"):
        create_archive(
            root=root,
            inputs=[root / "dist"],
            output=tmp_path / "bad.tar",
            fmt="tar",
            epoch=1_700_000_000,
            excludes=[],
            strip_prefix="other",
        )


def test_checksums_verify_and_detect_tampering(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    files = []
    for name in ("one.txt", "two.txt"):
        path = root / name
        path.write_text(name)
        files.append(path)
    checksums = root / "SHA256SUMS"
    hashes = write_checksums(files, checksums)
    assert set(hashes) == {"one.txt", "two.txt"}
    assert verify_checksums(checksums, root) == []
    (root / "one.txt").write_text("tampered")
    errors = verify_checksums(checksums, root)
    assert errors == ["SHA256SUMS:1: checksum mismatch for one.txt"]


def test_checksums_detect_escape_and_missing(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    (root / "SHA256SUMS").write_text(
        "0" * 64 + "  ../escape.txt\n0" * 64 + "  missing.txt\nmalformed\n",
        encoding="utf-8",
    )
    errors = verify_checksums(root / "SHA256SUMS", root)
    assert any("escapes artifact directory" in error for error in errors)
    assert any("missing file" in error for error in errors)
    assert any("malformed" in error for error in errors)
    assert verify_checksums(root / "absent", root) == ["checksum file is missing: absent"]


def fixture_context(tmp_path: Path) -> tuple[object, object, Path]:
    root = make_repo(
        tmp_path,
        files={"requirements.txt": "demo==1.0.0 --hash=sha256:" + "a" * 64 + "\n"},
    )
    config = load_config(root)
    return config, create_context(root, config), root


def test_manifest_sbom_and_provenance(tmp_path: Path) -> None:
    config, context, _root = fixture_context(tmp_path)
    artifacts_dir = context.paths.artifacts_dir
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    artifact = artifacts_dir / "package.tar.gz"
    artifact.write_bytes(b"archive")
    before = time.time()
    manifest, manifest_path = write_manifest(
        kind="package",
        config=config,
        context=context,
        artifacts=[artifact],
    )
    assert manifest["kind"] == "package"
    assert manifest["artifacts"][0]["sha256"]
    loaded = load_manifest(manifest_path)
    assert loaded["schema_version"] == manifest["schema_version"]

    sbom, warnings, sbom_path = build_sbom(config=config, context=context, artifacts=[artifact])
    assert warnings == []
    assert sbom["bomFormat"] == "CycloneDX"
    assert any(component["name"] == "demo" for component in sbom["components"])
    assert any(component["name"] == "package.tar.gz" for component in sbom["components"])
    assert sbom["serialNumber"].startswith("urn:uuid:")

    provenance, provenance_path = build_provenance(
        config=config,
        context=context,
        artifacts=[artifact],
        sbom_path=sbom_path,
        commands=["python -m build"],
    )
    assert provenance["predicateType"].startswith("https://slsa.dev/provenance/")
    assert provenance["subject"][0]["name"] == "package.tar.gz"
    assert provenance_path.is_file()
    assert before <= time.time()


def test_find_lockfiles_supported_names(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        files={
            "package-lock.json": "{}",
            "pnpm-lock.yaml": "lockfileVersion: 9",
            "go.sum": "",
        },
    )
    names = sorted(path.name for path in find_lockfiles(root))
    assert names == ["go.sum", "package-lock.json", "pnpm-lock.yaml"]


def test_verify_archive_extracts_and_rejects_unsafe(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    content = make_tree(root)
    good = tmp_path / "good.tar.gz"
    create_archive(
        root=root,
        inputs=[content],
        output=good,
        fmt="tar.gz",
        epoch=1_700_000_000,
        excludes=[],
        strip_prefix="dist",
    )
    problems = verify_archive(good, tmp_path / "extract")
    assert problems == []

    evil = tmp_path / "evil.tar"
    payload = tmp_path / "payload"
    payload.write_text("x")
    with tarfile.open(evil, "w") as archive:
        archive.add(payload, arcname="../escape.txt")
    problems = verify_archive(evil, tmp_path / "extract2")
    assert any("escapes" in problem for problem in problems)


def test_provenance_and_sbom_are_valid_json(tmp_path: Path) -> None:
    config, context, _ = fixture_context(tmp_path)
    context.paths.artifacts_dir.mkdir(parents=True, exist_ok=True)
    artifact = context.paths.artifacts_dir / "one.bin"
    artifact.write_bytes(b"1")
    _, _, sbom_path = build_sbom(config=config, context=context, artifacts=[artifact])
    _, provenance_path = build_provenance(
        config=config,
        context=context,
        artifacts=[artifact],
        sbom_path=sbom_path,
        commands=[],
    )
    assert json.loads(sbom_path.read_text())["bomFormat"] == "CycloneDX"
    assert json.loads(provenance_path.read_text())["_type"].endswith("Statement/v1")
