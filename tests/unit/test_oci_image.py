"""The OCI image writer: determinism, digest chain, and what tampering looks like."""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest

from ci_fabric.errors import CiError
from ci_fabric.oci import build_image, layout_digest, verify_layout
from ci_fabric.util import extract_archive

EPOCH = 1_793_777_169


def make_app(root: Path) -> Path:
    app = root / "app"
    (app / "bin").mkdir(parents=True)
    (app / "index.html").write_text("<html>fixture</html>\n", encoding="utf-8")
    script = app / "bin" / "run.sh"
    script.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    script.chmod(0o755)
    return app


def test_the_same_input_produces_the_same_image_bytes(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    first = build_image(app_dir=app, output_dir=tmp_path / "one", tag="fixture:1.0.0", epoch=EPOCH)
    second = build_image(app_dir=app, output_dir=tmp_path / "two", tag="fixture:1.0.0", epoch=EPOCH)

    assert first.manifest_digest == second.manifest_digest
    # The archive is a function of the input, not of the build: byte-identical.
    assert first.archive.read_bytes() == second.archive.read_bytes()
    assert first.layer_digest == second.layer_digest


def test_the_layout_is_a_valid_oci_image_layout(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    image = build_image(
        app_dir=app,
        output_dir=tmp_path / "out",
        tag="fixture:1.0.0",
        epoch=EPOCH,
        platform="linux/arm64",
        entrypoint=["/bin/run.sh"],
    )

    assert (image.layout / "oci-layout").is_file()
    assert json.loads((image.layout / "oci-layout").read_text())["imageLayoutVersion"] == "1.0.0"
    index = json.loads((image.layout / "index.json").read_text())
    descriptor = index["manifests"][0]
    assert descriptor["digest"] == image.manifest_digest
    assert descriptor["platform"] == {"architecture": "arm64", "os": "linux"}
    assert descriptor["annotations"]["org.opencontainers.image.ref.name"] == "fixture:1.0.0"

    config = json.loads(
        (image.layout / "blobs" / "sha256" / image.config_digest.split(":")[1]).read_text()
    )
    assert config["architecture"] == "arm64"
    assert config["os"] == "linux"
    assert config["config"] == {"Entrypoint": ["/bin/run.sh"]}
    assert config["rootfs"]["diff_ids"] == [image.layer_digest]
    assert verify_layout(image.layout, expected_manifest_digest=image.manifest_digest) == []
    assert layout_digest(image.layout) == image.manifest_digest


def test_the_layer_is_sorted_and_stamped_with_the_source_epoch(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    image = build_image(app_dir=app, output_dir=tmp_path / "out", tag="t:1", epoch=EPOCH)

    with tarfile.open(image.layout / "blobs" / "sha256" / image.layer_digest.split(":")[1]) as tar:
        members = tar.getmembers()

    names = [member.name for member in members]
    assert names == sorted(names)
    assert all(member.mtime == EPOCH for member in members)
    assert all(member.uid == 0 and member.gid == 0 for member in members)
    assert all(member.uname == "" and member.gname == "" for member in members)
    modes = {member.name: member.mode for member in members}
    assert modes["bin/run.sh"] == 0o755
    assert modes["index.html"] == 0o644


def test_the_archive_unpacks_into_the_layout(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    image = build_image(app_dir=app, output_dir=tmp_path / "out", tag="t:1", epoch=EPOCH)
    unpacked = tmp_path / "unpacked"
    # The project's own extractor: traversal-safe on every supported Python,
    # unlike tarfile's filter= parameter (3.12+/3.11.4+ only, CF-01).
    extract_archive(image.archive, unpacked)
    assert (unpacked / "oci-layout").is_file()
    assert (unpacked / "index.json").is_file()
    assert layout_digest(unpacked) == image.manifest_digest
    assert verify_layout(unpacked, expected_manifest_digest=image.manifest_digest) == []


def test_a_changed_layer_fails_verification(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    image = build_image(app_dir=app, output_dir=tmp_path / "out", tag="t:1", epoch=EPOCH)
    layer = image.layout / "blobs" / "sha256" / image.layer_digest.split(":")[1]
    layer.write_bytes(layer.read_bytes() + b"tampered")

    errors = verify_layout(image.layout, expected_manifest_digest=image.manifest_digest)
    assert any("hashes to" in error for error in errors)
    assert any("diff_ids" in error for error in errors)


def test_a_drifted_index_digest_is_reported(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    image = build_image(app_dir=app, output_dir=tmp_path / "out", tag="t:1", epoch=EPOCH)

    errors = verify_layout(image.layout, expected_manifest_digest="sha256:" + "0" * 64)
    assert any("drifted" in error for error in errors)


def test_a_missing_marker_is_not_a_layout(tmp_path: Path) -> None:
    assert verify_layout(tmp_path / "absent") == [
        f"missing oci-layout marker in {tmp_path / 'absent'}"
    ]


def test_an_invalid_platform_is_refused(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    with pytest.raises(CiError, match="invalid image platform"):
        build_image(
            app_dir=app, output_dir=tmp_path / "out", tag="t:1", epoch=EPOCH, platform="amd64"
        )
