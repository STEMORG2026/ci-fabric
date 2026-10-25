"""Deterministic packaging and supply-chain metadata.

Implemented controls:

* deterministic tar/zip archives with fixed metadata;
* SHA-256 checksums and artifact manifests;
* CycloneDX 1.5 SBOM from lockfiles where a parser exists;
* SLSA-provenance-shaped JSON describing inputs, builder, commands and outputs.

Not implemented: signing, remote attestation, and independent rebuild
verification. The documentation states this explicitly.
"""

from __future__ import annotations

import contextlib
import gzip
import io
import json
import os
import re
import tarfile
import uuid
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

from .config import Config
from .context import Context
from .errors import CiError
from .util import (
    archive_kind,
    ensure_dir,
    file_sha256,
    hash_stream,
    is_within,
    iter_files,
    load_toml,
    matches_any,
    read_text,
    relpath,
    sha256_bytes,
    sha256_file,
    write_json,
    write_text_atomic,
)

SBOM_VERSION = "1.5"
MANIFEST_SCHEMA = 1
_MTIME_FLOOR = 315_532_800  # 1980-01-01, valid in both tar and zip
_COMPONENT_PATTERNS = (
    "requirements*.txt",
    "requirements/*.txt",
    "*/requirements*.txt",
    "uv.lock",
    "poetry.lock",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "Cargo.lock",
    "go.sum",
)


@dataclass(slots=True)
class ArchiveResult:
    path: Path
    entries: list[str]
    sha256: str
    size: int


@dataclass(slots=True)
class Component:
    name: str
    version: str
    ecosystem: str
    hashes: list[dict[str, str]]
    source: str

    @property
    def purl(self) -> str:
        if self.ecosystem == "npm":
            encoded = quote(self.name, safe="")
            return f"pkg:npm/{encoded}@{quote(self.version, safe='')}"
        if self.ecosystem == "cargo":
            return f"pkg:cargo/{quote(self.name, safe='')}@{quote(self.version, safe='')}"
        if self.ecosystem == "golang":
            return f"pkg:golang/{quote(self.name, safe='')}@{quote(self.version, safe='')}"
        normalized = re.sub(r"[-_.]+", "-", self.name).lower()
        return f"pkg:pypi/{quote(normalized, safe='')}@{quote(self.version, safe='')}"


class _DeterministicWriter:
    def __init__(self, output: Path, fmt: str, epoch: int) -> None:
        self.output = output
        self.fmt = fmt
        self.epoch = max(epoch, _MTIME_FLOOR)

    def write(self, entries: list[tuple[str, Path]]) -> None:
        ensure_dir(self.output.parent)
        if self.fmt in {"tar", "tar.gz"}:
            self._write_tar(entries)
        elif self.fmt == "zip":
            self._write_zip(entries)
        else:  # pragma: no cover - validated by config
            raise CiError(f"unsupported archive format: {self.fmt}")

    def _write_tar(self, entries: list[tuple[str, Path]]) -> None:
        raw = self.output.open("wb")
        try:
            if self.fmt == "tar.gz":
                gzip_file = gzip.GzipFile(
                    filename="",
                    mode="wb",
                    fileobj=raw,
                    compresslevel=9,
                    mtime=0,
                )
                with gzip_file, tarfile.open(fileobj=gzip_file, mode="w") as archive:
                    for name, source in entries:
                        info = tarfile.TarInfo(name=name)
                        info.size = source.stat().st_size
                        info.mtime = self.epoch
                        info.uid = 0
                        info.gid = 0
                        info.uname = ""
                        info.gname = ""
                        info.mode = 0o755 if os.access(source, os.X_OK) else 0o644
                        info.type = tarfile.REGTYPE
                        with source.open("rb") as handle:
                            archive.addfile(info, handle)
            else:
                with raw, tarfile.open(fileobj=raw, mode="w") as archive:
                    for name, source in entries:
                        info = tarfile.TarInfo(name=name)
                        info.size = source.stat().st_size
                        info.mtime = self.epoch
                        info.uid = 0
                        info.gid = 0
                        info.uname = ""
                        info.gname = ""
                        info.mode = 0o755 if os.access(source, os.X_OK) else 0o644
                        info.type = tarfile.REGTYPE
                        with source.open("rb") as handle:
                            archive.addfile(info, handle)
        finally:
            raw.close()

    def _write_zip(self, entries: list[tuple[str, Path]]) -> None:
        with zipfile.ZipFile(
            self.output,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
            allowZip64=True,
        ) as archive:
            for name, source in entries:
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                mode = 0o755 if os.access(source, os.X_OK) else 0o644
                info.external_attr = (mode & 0xFFFF) << 16
                info.create_system = 3
                info.flag_bits |= 0x800
                with source.open("rb") as handle:
                    archive.writestr(info, handle.read(), compress_type=zipfile.ZIP_DEFLATED)


def _matches(name: str, patterns: Iterable[str]) -> bool:
    return matches_any(name, patterns)


def _archive_entries(
    root: Path,
    inputs: Sequence[Path],
    *,
    excludes: Sequence[str],
    strip_prefix: str | None,
    prefix: str | None,
) -> list[tuple[str, Path]]:
    base = (root / strip_prefix).resolve() if strip_prefix else root.resolve()
    entries: dict[str, Path] = {}
    for input_path in inputs:
        source = input_path.resolve()
        if not is_within(source, root):
            raise CiError(f"package input is outside the repository: {input_path}")
        if strip_prefix and not is_within(source, base):
            raise CiError(f"package input {input_path} is outside strip_prefix {strip_prefix!r}")
        candidates: list[Path]
        if source.is_file():
            candidates = [source]
        elif source.is_dir():
            candidates = list(
                iter_files(
                    source,
                    excluded_dirs=(
                        ".git",
                        ".ci",
                        ".localci",
                        "__pycache__",
                        "node_modules",
                        ".venv",
                        "venv",
                    ),
                )
            )
        else:
            raise CiError(f"package input does not exist: {input_path}")
        for candidate in candidates:
            relative = candidate.relative_to(base).as_posix()
            archive_name = f"{prefix.strip('/')}/{relative}" if prefix else relative
            archive_name = archive_name.lstrip("./")
            if (
                not archive_name
                or archive_name.startswith("/")
                or ".." in PurePosixPath(archive_name).parts
            ):
                raise CiError(f"unsafe archive entry name: {archive_name!r}")
            if _matches(relative, excludes) or _matches(archive_name, excludes):
                continue
            if archive_name in entries:
                raise CiError(f"duplicate archive entry: {archive_name}")
            entries[archive_name] = candidate
    if not entries:
        raise CiError(
            "no files matched the package inputs",
            remediation="Check [stages.package].inputs, strip_prefix, and exclude.",
        )
    return sorted(entries.items(), key=lambda item: item[0])


def create_archive(
    *,
    root: Path,
    inputs: Sequence[Path],
    output: Path,
    fmt: str,
    epoch: int,
    excludes: Sequence[str],
    strip_prefix: str | None = None,
    prefix: str | None = None,
) -> ArchiveResult:
    entries = _archive_entries(
        root,
        inputs,
        excludes=excludes,
        strip_prefix=strip_prefix,
        prefix=prefix,
    )
    writer = _DeterministicWriter(output, fmt, epoch)
    writer.write(entries)
    return ArchiveResult(
        path=output,
        entries=[name for name, _ in entries],
        sha256=sha256_file(output),
        size=output.stat().st_size,
    )


def media_type(path: Path) -> str:
    suffix = path.name.lower()
    if suffix.endswith((".tar.gz", ".tgz")):
        return "application/gzip"
    if suffix.endswith(".tar"):
        return "application/x-tar"
    if suffix.endswith((".zip", ".whl")):
        return "application/zip"
    if suffix.endswith(".json"):
        return "application/json"
    if suffix.endswith(".md"):
        return "text/markdown"
    if suffix.endswith(".txt"):
        return "text/plain"
    return "application/octet-stream"


def artifact_descriptor(base: Path, path: Path) -> dict[str, Any]:
    digest, size = file_sha256(path)
    return {
        "name": path.name,
        "path": relpath(path, base),
        "sha256": digest,
        "size": size,
        "kind": "archive" if archive_kind(path) else "file",
        "media_type": media_type(path),
    }


def write_checksums(files: Sequence[Path], path: Path) -> dict[str, str]:
    hashes = {
        candidate.name: sha256_file(candidate) for candidate in sorted(files, key=lambda p: p.name)
    }
    lines = [f"{digest}  {name}" for name, digest in sorted(hashes.items())]
    write_text_atomic(path, "\n".join(lines) + "\n")
    return hashes


def verify_checksums(path: Path, base: Path) -> list[str]:
    errors: list[str] = []
    if not path.is_file():
        return [f"checksum file is missing: {relpath(path, base)}"]
    for line_number, line in enumerate(read_text(path).splitlines(), start=1):
        if not line.strip():
            continue
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            errors.append(f"{path.name}:{line_number}: malformed checksum line")
            continue
        digest, name = parts[0], parts[1].strip()
        candidate = (path.parent / name).resolve()
        if not is_within(candidate, base):
            errors.append(f"{path.name}:{line_number}: path escapes artifact directory: {name}")
            continue
        if not candidate.is_file():
            errors.append(f"{path.name}:{line_number}: missing file: {name}")
            continue
        actual = sha256_file(candidate)
        if actual != digest:
            errors.append(f"{path.name}:{line_number}: checksum mismatch for {name}")
    return errors


def write_manifest(
    *,
    kind: str,
    config: Config,
    context: Context,
    artifacts: Sequence[Path],
    extra: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], Path]:
    base = context.paths.artifacts_dir
    descriptors = [artifact_descriptor(base, item) for item in artifacts if item.is_file()]
    descriptors.sort(key=lambda item: item["path"])
    manifest: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA,
        "kind": kind,
        "project": config.project.name,
        "version": config.project.version,
        "revision": context.git.revision,
        "dirty": context.git.dirty,
        "run_id": context.run_id,
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "source_date_epoch": context.source_date_epoch,
        "artifacts": descriptors,
    }
    if extra:
        manifest.update(extra)
    path = base / f"{kind}-manifest.json"
    write_json(path, manifest)
    return manifest, path


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CiError(f"artifact manifest not found: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise CiError(f"invalid artifact manifest {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != MANIFEST_SCHEMA:
        raise CiError(f"unsupported artifact manifest schema in {path}")
    if data.get("kind") not in {"build", "package"}:
        raise CiError(f"invalid artifact manifest kind in {path}")
    return data


def _parse_requirements(path: Path) -> list[Component]:
    components: list[Component] = []
    for raw in read_text(path).splitlines():
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith(("#", "-", "git+", "http:", "https:", "file:")):
            continue
        match = re.match(r"^([A-Za-z0-9_.-]+)\s*==\s*([A-Za-z0-9_.!+~-]+)", line)
        if not match:
            continue
        name, version = match.groups()
        hashes = re.findall(r"--hash=sha256:([0-9a-fA-F]{64})", raw)
        components.append(
            Component(
                name=name,
                version=version,
                ecosystem="pypi",
                hashes=[{"alg": "SHA-256", "content": value.lower()} for value in hashes],
                source=relpath(path, path.parents[1]) if len(path.parents) > 1 else path.name,
            )
        )
    return components


def _parse_uv_lock(path: Path) -> list[Component]:
    data = load_toml(path)
    packages = data.get("package", [])
    components: list[Component] = []
    if not isinstance(packages, list):
        return components
    for item in packages:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        version = item.get("version")
        if isinstance(name, str) and isinstance(version, str):
            components.append(
                Component(
                    name=name,
                    version=version,
                    ecosystem="pypi",
                    hashes=[],
                    source=path.name,
                )
            )
    return components


def _parse_poetry_lock(path: Path) -> list[Component]:
    data = load_toml(path)
    packages = data.get("package", [])
    components: list[Component] = []
    if not isinstance(packages, list):
        return components
    for item in packages:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        version = item.get("version")
        if isinstance(name, str) and isinstance(version, str):
            components.append(
                Component(
                    name=name,
                    version=version,
                    ecosystem="pypi",
                    hashes=[],
                    source=path.name,
                )
            )
    return components


def _parse_package_lock(path: Path) -> list[Component]:
    data = json.loads(read_text(path))
    components: list[Component] = []
    packages = data.get("packages", {}) if isinstance(data, dict) else {}
    if isinstance(packages, dict):
        for location, metadata in packages.items():
            if not location or not isinstance(metadata, dict):
                continue
            name = metadata.get("name")
            version = metadata.get("version")
            if not isinstance(name, str):
                parts = location.split("node_modules/")
                name = parts[-1] if parts else location
            if isinstance(name, str) and isinstance(version, str):
                integrity = metadata.get("integrity")
                hashes = []
                if isinstance(integrity, str) and integrity.startswith("sha512-"):
                    import base64
                    import binascii

                    with contextlib.suppress(binascii.Error):
                        hashes.append(
                            {
                                "alg": "SHA-512",
                                "content": base64.b64decode(integrity[7:]).hex(),
                            }
                        )
                components.append(
                    Component(
                        name=name,
                        version=version,
                        ecosystem="npm",
                        hashes=hashes,
                        source=path.name,
                    )
                )
    return components


_PNPM_ENTRY = re.compile(r"^  '?([^':]+)'?:\s*$")


def _split_pnpm_key(key: str) -> tuple[str, str] | None:
    key = key.strip().strip("'\"")
    if key.startswith("/"):
        key = key[1:]
    marker = key.rfind("@")
    if marker <= 0:
        return None
    name, version = key[:marker], key[marker + 1 :]
    version = version.split("(", 1)[0]
    if not name or not version or "/" in version:
        return None
    return name, version


def _parse_pnpm_lock(path: Path) -> list[Component]:
    components: list[Component] = []
    in_packages = False
    for line in read_text(path).splitlines():
        if line.strip() in {"packages:", "snapshots:"}:
            in_packages = True
            continue
        if in_packages and line and not line.startswith(" "):
            in_packages = False
        if not in_packages:
            continue
        match = _PNPM_ENTRY.match(line)
        if not match:
            continue
        parsed = _split_pnpm_key(match.group(1))
        if parsed is None:
            continue
        name, version = parsed
        components.append(
            Component(name=name, version=version, ecosystem="npm", hashes=[], source=path.name)
        )
    return components


def _parse_yarn_lock(path: Path) -> list[Component]:
    components: list[Component] = []
    current_name: str | None = None
    for raw in read_text(path).splitlines():
        if not raw.startswith(" ") and raw.rstrip().endswith(":"):
            header = raw.rstrip()[:-1].split(",")[0].strip().strip('"')
            if "@" in header:
                if header.startswith("@"):
                    parts = header.split("@", 2)
                    current_name = "@" + parts[1] if len(parts) > 1 else None
                else:
                    current_name = header.split("@", 1)[0]
        elif current_name and raw.startswith("  version"):
            match = re.search(r'version[:\s]+"?([^"\s]+)"?', raw)
            if match:
                version = match.group(1)
                components.append(
                    Component(
                        name=current_name,
                        version=version,
                        ecosystem="npm",
                        hashes=[],
                        source=path.name,
                    )
                )
                current_name = None
    return components


def _parse_cargo_lock(path: Path) -> list[Component]:
    data = load_toml(path)
    components: list[Component] = []
    packages = data.get("package", [])
    if isinstance(packages, list):
        for item in packages:
            if isinstance(item, dict):
                name, version = item.get("name"), item.get("version")
                if isinstance(name, str) and isinstance(version, str):
                    components.append(
                        Component(
                            name=name,
                            version=version,
                            ecosystem="cargo",
                            hashes=[],
                            source=path.name,
                        )
                    )
    return components


def _parse_go_sum(path: Path) -> list[Component]:
    seen: set[tuple[str, str]] = set()
    components: list[Component] = []
    for line in read_text(path).splitlines():
        parts = line.split()
        if len(parts) < 3 or parts[0].endswith("/go.mod"):
            continue
        module, version = parts[0], parts[1]
        if (module, version) in seen:
            continue
        seen.add((module, version))
        components.append(
            Component(
                name=module,
                version=version,
                ecosystem="golang",
                hashes=[],
                source=path.name,
            )
        )
    return components


def find_lockfiles(root: Path) -> list[Path]:
    found: set[Path] = set()
    for pattern in _COMPONENT_PATTERNS:
        for candidate in root.glob(pattern):
            if candidate.is_file() and not any(
                part in {".venv", "venv", "node_modules", ".git"} for part in candidate.parts
            ):
                found.add(candidate.resolve())
    return sorted(found)


def collect_components(root: Path) -> tuple[list[Component], list[str]]:
    components: list[Component] = []
    warnings: list[str] = []
    for lockfile in find_lockfiles(root):
        try:
            if lockfile.name == "uv.lock":
                components.extend(_parse_uv_lock(lockfile))
            elif lockfile.name == "poetry.lock":
                components.extend(_parse_poetry_lock(lockfile))
            elif lockfile.name == "package-lock.json":
                components.extend(_parse_package_lock(lockfile))
            elif lockfile.name == "pnpm-lock.yaml":
                components.extend(_parse_pnpm_lock(lockfile))
            elif lockfile.name == "yarn.lock":
                components.extend(_parse_yarn_lock(lockfile))
            elif lockfile.name == "Cargo.lock":
                components.extend(_parse_cargo_lock(lockfile))
            elif lockfile.name == "go.sum":
                components.extend(_parse_go_sum(lockfile))
            elif lockfile.name.startswith("requirements") or "requirements" in lockfile.name:
                components.extend(_parse_requirements(lockfile))
            else:
                warnings.append(f"no SBOM parser for {relpath(lockfile, root)}")
        except (OSError, ValueError, json.JSONDecodeError, CiError) as exc:
            warnings.append(f"could not parse {relpath(lockfile, root)}: {exc}")
    deduplicated: dict[str, Component] = {}
    for component in components:
        if not component.name or not component.version:
            continue
        key = component.purl
        if key not in deduplicated or (not deduplicated[key].hashes and component.hashes):
            deduplicated[key] = component
    return sorted(deduplicated.values(), key=lambda item: item.purl), warnings


def build_sbom(
    *,
    config: Config,
    context: Context,
    artifacts: Sequence[Path],
) -> tuple[dict[str, Any], list[str], Path]:
    components, warnings = collect_components(context.root)
    artifact_components: list[dict[str, Any]] = []
    for item in sorted(artifacts, key=lambda path: path.name):
        digest = sha256_file(item)
        artifact_components.append(
            {
                "type": "file",
                "bom-ref": f"file:{item.name}:{digest[:12]}",
                "name": item.name,
                "version": config.project.version or "unversioned",
                "hashes": [{"alg": "SHA-256", "content": digest}],
                "properties": [
                    {"name": "localci:size", "value": str(item.stat().st_size)},
                    {"name": "localci:media_type", "value": media_type(item)},
                ],
            }
        )
    sbom_components: list[dict[str, Any]] = []
    for component in components:
        entry: dict[str, Any] = {
            "type": "library",
            "bom-ref": component.purl,
            "name": component.name,
            "version": component.version,
            "purl": component.purl,
            "properties": [{"name": "localci:inventory_source", "value": component.source}],
        }
        if component.hashes:
            entry["hashes"] = component.hashes
        sbom_components.append(entry)
    sbom_components.extend(artifact_components)
    lockfiles = find_lockfiles(context.root)
    lock_properties = []
    materials = []
    for lockfile in lockfiles:
        digest = sha256_file(lockfile)
        name = relpath(lockfile, context.root)
        lock_properties.append({"name": f"localci:lockfile:{name}", "value": digest})
        materials.append({"uri": f"file:{name}", "digest": {"sha256": digest}})
    seed = (
        f"{config.project.name}:{config.project.version or 'unversioned'}:"
        f"{context.git.revision or 'nogit'}:"
        + ",".join(sorted(relpath(item, context.paths.artifacts_dir) for item in artifacts))
    )
    serial = str(uuid.uuid5(uuid.NAMESPACE_URL, f"ci-fabric:{seed}"))
    timestamp = (
        datetime.fromtimestamp(max(context.source_date_epoch, _MTIME_FLOOR), tz=UTC)
        .isoformat()
        .replace("+00:00", "Z")
    )
    sbom: dict[str, Any] = {
        "bomFormat": "CycloneDX",
        "specVersion": SBOM_VERSION,
        "serialNumber": f"urn:uuid:{serial}",
        "version": 1,
        "metadata": {
            "timestamp": timestamp,
            "tools": {
                "components": [
                    {
                        "type": "application",
                        "supplier": {"name": "ci-fabric"},
                        "name": "ci-fabric",
                        "version": "0.1.0",
                    }
                ]
            },
            "component": {
                "type": "application",
                "name": config.project.name,
                "version": config.project.version or "unversioned",
            },
            "properties": [
                {"name": "localci:revision", "value": context.git.revision or "nogit"},
                {"name": "localci:dirty", "value": "true" if context.git.dirty else "false"},
                {
                    "name": "localci:source_date_epoch",
                    "value": str(context.source_date_epoch),
                },
                *lock_properties,
            ],
        },
        "components": sbom_components,
    }
    if materials:
        sbom["metadata"]["materials"] = materials
    path = context.paths.artifacts_dir / "sbom.cdx.json"
    write_json(path, sbom)
    return sbom, warnings, path


def build_provenance(
    *,
    config: Config,
    context: Context,
    artifacts: Sequence[Path],
    sbom_path: Path,
    commands: Sequence[str],
) -> tuple[dict[str, Any], Path]:
    subjects = [
        {
            "name": item.name,
            "digest": {"sha256": sha256_file(item)},
        }
        for item in sorted(artifacts, key=lambda path: path.name)
    ]
    dependencies = [
        {
            "uri": f"file:{relpath(lockfile, context.root)}",
            "digest": {"sha256": sha256_file(lockfile)},
        }
        for lockfile in find_lockfiles(context.root)
    ]
    provenance: dict[str, Any] = {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": subjects,
        "predicateType": "https://slsa.dev/provenance/v1",
        "predicate": {
            "buildDefinition": {
                "buildType": "https://ci-fabric.local/build/v1",
                "externalParameters": {
                    "project": config.project.name,
                    "version": config.project.version,
                    "profile": config.policy.profile,
                    "revision": context.git.revision,
                    "clean_tree": not context.git.dirty,
                },
                "internalParameters": {
                    "run_id": context.run_id,
                    "source_date_epoch": context.source_date_epoch,
                    "clean_environment": context.clean_env,
                    "environment": {
                        key: value
                        for key, value in context.environment_snapshot()["environment"].items()
                        if value is not None
                    },
                    "tools": context.tool_versions,
                    "commands": list(commands),
                    "sbom_sha256": sha256_file(sbom_path),
                },
                "resolvedDependencies": dependencies,
            },
            "runDetails": {
                "builder": {"id": "ci-fabric/0.1.0"},
                "metadata": {
                    "invocationId": context.run_id,
                    "startedOn": context.started_at.isoformat().replace("+00:00", "Z"),
                    "finishedOn": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                },
                "byproducts": [
                    {
                        "name": sbom_path.name,
                        "digest": {"sha256": sha256_file(sbom_path)},
                    }
                ],
            },
        },
    }
    path = context.paths.artifacts_dir / "provenance.json"
    write_json(path, provenance)
    return provenance, path


def verify_archive(path: Path, destination: Path) -> list[str]:
    """Extract an archive and return integrity problems (empty means valid)."""
    from .util import extract_archive

    try:
        extract_archive(path, destination)
    except CiError as exc:
        return [str(exc)]
    return []


def archive_entries(path: Path) -> list[str]:
    kind = archive_kind(path)
    if kind is None:
        return []
    if kind.startswith("tar"):
        with tarfile.open(path, "r:*") as archive:
            return [member.name for member in archive.getmembers() if member.isfile()]
    with zipfile.ZipFile(path) as archive:
        return [info.filename for info in archive.infolist() if not info.is_dir()]


def archive_kind_supported(path: Path) -> bool:
    return archive_kind(path) is not None


def hash_lockfiles(root: Path) -> list[dict[str, str]]:
    return [
        {
            "path": relpath(path, root),
            "sha256": sha256_file(path),
        }
        for path in find_lockfiles(root)
    ]


def write_sbom_text(sbom: dict[str, Any]) -> str:
    return json.dumps(sbom, indent=2, sort_keys=True) + "\n"


def archive_digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hash_stream(handle)


def read_checksum_file(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.is_file():
        return result
    for line in read_text(path).splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) == 2:
            result[parts[1].strip()] = parts[0]
    return result


def directory_digest(directory: Path) -> str:
    """Deterministic digest of a directory tree (names + file contents)."""
    digest = io.BytesIO()
    for path in sorted(iter_files(directory), key=lambda item: item.as_posix()):
        relative = path.relative_to(directory).as_posix()
        digest.write(relative.encode("utf-8"))
        digest.write(b"\0")
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.write(chunk)
        digest.write(b"\0")
    return sha256_bytes(digest.getvalue())


def ensure_artifact_path(path: Path, base: Path) -> None:
    if not is_within(path, base):
        raise CiError(f"artifact path escapes the run directory: {path}")


def sha256sum_name(path: Path) -> str:
    return sha256_file(path)


def parse_epoch(value: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise CiError(f"invalid epoch: {value}") from exc


@dataclass(slots=True)
class LockfileInventory:
    path: str
    sha256: str


def lockfile_inventory(root: Path) -> list[LockfileInventory]:
    return [
        LockfileInventory(path=relpath(path, root), sha256=sha256_file(path))
        for path in find_lockfiles(root)
    ]
