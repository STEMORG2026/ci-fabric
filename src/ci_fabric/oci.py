"""Deterministic OCI image layouts, built without a container runtime.

An OCI image is three kinds of bytes: a layer (a tar of the filesystem), a
config JSON and a manifest that names both by digest — plus an index and an
``oci-layout`` marker. Producing them needs no daemon, which is what makes this
a fit for a local-first deploy strategy: the image is built from the same
verified release artifact the other strategies deploy, under the same
determinism rules as the package archive (sorted entries, ``SOURCE_DATE_EPOCH``
mtimes, no host uid/gid), and the digest chain is re-verified before any runtime
sees it.

Blobs live under ``<layout>/blobs/sha256/`` per OCI image layout 1.0. Layers are
written uncompressed (``application/vnd.oci.image.layer.v1.tar``): gzip would
add a second digest to reconcile (``diff_id`` vs ``digest``) for no gain in a
local tool, and an uncompressed tar is byte-identical on every machine.

Loading or pushing the image is the one step that genuinely needs a runtime, so
that is the bring-your-own ``image_command`` hook — ci-fabric builds and proves
the bytes, the runtime consumes them.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import CiError
from .supplychain import create_archive
from .util import ensure_dir, sha256_file, write_json

LAYOUT_VERSION = "1.0.0"
MEDIA_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
MEDIA_INDEX = "application/vnd.oci.image.index.v1+json"
MEDIA_CONFIG = "application/vnd.oci.image.config.v1+json"
MEDIA_LAYER = "application/vnd.oci.image.layer.v1.tar"

DEFAULT_PLATFORM = "linux/amd64"
_PLATFORMS = {"linux/amd64", "linux/arm64"}
_DIGEST_PREFIX = "sha256:"


@dataclass(slots=True)
class OciImage:
    layout: Path
    archive: Path
    tag: str
    platform: str
    manifest_digest: str
    config_digest: str
    layer_digest: str
    layer_size: int

    @property
    def reference(self) -> str:
        """The immutable reference a runtime can be handed."""
        return f"{self.tag}@{self.manifest_digest}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "tag": self.tag,
            "reference": self.reference,
            "platform": self.platform,
            "manifest_digest": self.manifest_digest,
            "config_digest": self.config_digest,
            "layer_digest": self.layer_digest,
            "layer_size": self.layer_size,
        }


def _digest(data: bytes) -> str:
    return _DIGEST_PREFIX + hashlib.sha256(data).hexdigest()


def _blob_json(document: dict[str, Any]) -> bytes:
    """Canonical JSON: the same document must always be the same bytes."""
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _store(layout: Path, data: bytes) -> tuple[str, int]:
    digest = _digest(data)
    path = layout / "blobs" / "sha256" / digest[len(_DIGEST_PREFIX) :]
    ensure_dir(path.parent)
    path.write_bytes(data)
    return digest, len(data)


def _created(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _platform_parts(platform: str) -> tuple[str, str]:
    os_name, _, architecture = platform.partition("/")
    if not os_name or not architecture:
        raise CiError(
            f"invalid image platform: {platform!r}",
            remediation="Use os/arch, for example linux/amd64 or linux/arm64.",
        )
    return os_name, architecture


def build_image(
    *,
    app_dir: Path,
    output_dir: Path,
    tag: str,
    epoch: int,
    platform: str = DEFAULT_PLATFORM,
    entrypoint: Sequence[str] = (),
    excludes: Sequence[str] = (),
    created_by: str = "ci-fabric",
) -> OciImage:
    """Build ``<output_dir>/image`` (OCI layout) and ``<output_dir>/image.tar``."""
    if not app_dir.is_dir():
        raise CiError(f"image content directory is missing: {app_dir}")
    os_name, architecture = _platform_parts(platform)
    layout = output_dir / "image"
    archive = output_dir / "image.tar"
    for path in (layout, archive):
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
    ensure_dir(layout)

    # The layer is the application content, archived by the same deterministic
    # writer the package stage uses — sorted entries, epoch mtimes, uid/gid 0.
    layer_path = layout / "blobs" / "sha256" / ".layer.tmp"
    ensure_dir(layer_path.parent)
    created = create_archive(
        root=app_dir,
        inputs=[app_dir],
        output=layer_path,
        fmt="tar",
        epoch=epoch,
        excludes=excludes,
    )
    layer_digest = _DIGEST_PREFIX + created.sha256
    final_layer = layout / "blobs" / "sha256" / created.sha256
    layer_path.replace(final_layer)
    layer_size = final_layer.stat().st_size

    config_document: dict[str, Any] = {
        "created": _created(epoch),
        "architecture": architecture,
        "os": os_name,
        "config": {"Entrypoint": list(entrypoint)} if entrypoint else {},
        "rootfs": {"type": "layers", "diff_ids": [layer_digest]},
        "history": [{"created": _created(epoch), "created_by": created_by}],
    }
    config_digest, config_size = _store(layout, _blob_json(config_document))

    manifest_document = {
        "schemaVersion": 2,
        "mediaType": MEDIA_MANIFEST,
        "config": {
            "mediaType": MEDIA_CONFIG,
            "digest": config_digest,
            "size": config_size,
        },
        "layers": [{"mediaType": MEDIA_LAYER, "digest": layer_digest, "size": layer_size}],
    }
    manifest_digest, manifest_size = _store(layout, _blob_json(manifest_document))

    index_document = {
        "schemaVersion": 2,
        "mediaType": MEDIA_INDEX,
        "manifests": [
            {
                "mediaType": MEDIA_MANIFEST,
                "digest": manifest_digest,
                "size": manifest_size,
                "platform": {"architecture": architecture, "os": os_name},
                "annotations": {"org.opencontainers.image.ref.name": tag},
            }
        ],
    }
    write_json(layout / "index.json", index_document)
    write_json(layout / "oci-layout", {"imageLayoutVersion": LAYOUT_VERSION})

    # The archive is the layout directory itself — that is the OCI archive
    # format a runtime accepts — written deterministically so the same input
    # yields the same bytes on any machine.
    create_archive(
        root=layout,
        inputs=[layout],
        output=archive,
        fmt="tar",
        epoch=epoch,
        excludes=(),
    )

    return OciImage(
        layout=layout,
        archive=archive,
        tag=tag,
        platform=platform,
        manifest_digest=manifest_digest,
        config_digest=config_digest,
        layer_digest=layer_digest,
        layer_size=layer_size,
    )


def _read_blob(
    layout: Path, digest: str, *, expected_size: int | None
) -> tuple[bytes | None, str | None]:
    if not digest.startswith(_DIGEST_PREFIX):
        return None, f"blob digest is not sha256: {digest!r}"
    path = layout / "blobs" / "sha256" / digest[len(_DIGEST_PREFIX) :]
    if not path.is_file():
        return None, f"missing blob {digest}"
    data = path.read_bytes()
    actual = _digest(data)
    if actual != digest:
        return None, f"blob {digest} hashes to {actual}"
    if expected_size is not None and len(data) != expected_size:
        return None, f"blob {digest} is {len(data)} bytes, manifest says {expected_size}"
    return data, None


def verify_layout(layout: Path, *, expected_manifest_digest: str | None = None) -> list[str]:
    """Verify an OCI layout's digest chain. An empty list means it is intact."""
    errors: list[str] = []
    marker = layout / "oci-layout"
    index_path = layout / "index.json"
    if not marker.is_file():
        return [f"missing oci-layout marker in {layout}"]
    try:
        marker_document = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"unreadable oci-layout marker: {exc}"]
    if marker_document.get("imageLayoutVersion") != LAYOUT_VERSION:
        errors.append(f"unsupported layout version: {marker_document.get('imageLayoutVersion')!r}")
    if not index_path.is_file():
        return [*errors, "missing index.json"]
    try:
        index_document = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [*errors, f"unreadable index.json: {exc}"]
    manifests = index_document.get("manifests")
    if not isinstance(manifests, list) or not manifests:
        return [*errors, "index.json lists no manifests"]

    for descriptor in manifests:
        if not isinstance(descriptor, dict):
            errors.append("index.json manifest descriptor is malformed")
            continue
        digest = str(descriptor.get("digest", ""))
        if expected_manifest_digest is not None and digest != expected_manifest_digest:
            errors.append(
                f"image digest drifted: index says {digest}, record says {expected_manifest_digest}"
            )
        data, error = _read_blob(layout, digest, expected_size=descriptor.get("size"))
        if error:
            errors.append(error)
            continue
        try:
            manifest = json.loads(data or b"{}")
        except ValueError as exc:
            errors.append(f"unreadable manifest {digest}: {exc}")
            continue
        if manifest.get("mediaType") != MEDIA_MANIFEST:
            errors.append(f"manifest {digest} has unexpected mediaType")
        config = manifest.get("config")
        layers = manifest.get("layers")
        if not isinstance(config, dict) or not isinstance(layers, list) or not layers:
            errors.append(f"manifest {digest} has no config or layers")
            continue
        config_data, error = _read_blob(
            layout, str(config.get("digest", "")), expected_size=config.get("size")
        )
        if error:
            errors.append(error)
            continue
        diff_ids: list[str] = []
        for layer in layers:
            if not isinstance(layer, dict):
                errors.append(f"manifest {digest} has a malformed layer descriptor")
                continue
            layer_data, error = _read_blob(
                layout, str(layer.get("digest", "")), expected_size=layer.get("size")
            )
            if error:
                errors.append(error)
            else:
                # Uncompressed layers: the blob digest is the diff_id.
                diff_ids.append(_digest(layer_data or b""))
        try:
            config_document = json.loads(config_data or b"{}")
        except ValueError as exc:
            errors.append(f"unreadable config {config.get('digest')}: {exc}")
            continue
        recorded = (config_document.get("rootfs") or {}).get("diff_ids")
        if recorded != diff_ids:
            errors.append("config diff_ids do not match the layer blobs")
        if not config_document.get("architecture") or not config_document.get("os"):
            errors.append("config does not declare an architecture and os")
    return errors


def layout_digest(layout: Path) -> str | None:
    """The manifest digest an intact layout points at, or None when it has none."""
    index_path = layout / "index.json"
    try:
        document = json.loads(index_path.read_text(encoding="utf-8"))
        manifests = document["manifests"]
        return str(manifests[0]["digest"])
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return None


def archive_digest(archive: Path) -> str | None:
    return sha256_file(archive) if archive.is_file() else None
