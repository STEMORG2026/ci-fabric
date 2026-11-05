"""Content-addressed build cache.

A build whose inputs are identical should be built once. The cache key is
the honest set of things a build depends on that ci-fabric can pin:

* the **git revision** — on a clean tree it pins every tracked byte;
* the **rendered build commands** — the same tree built by a different
  command, toolchain path, or configuration is a different build.

Anything the key cannot see (untracked files, environment outside the
command line) is why the cache only engages on **clean trees** and why
`[stages.build].cache` is opt-in. A dirty tree is never looked up and never
stored: a build of a tree nobody can name again is not worth keeping.

Entries store the declared build outputs plus their SHA-256 digests; a
restore re-verifies every digest before touching the working tree, so a
corrupted cache is a miss, never a wrong artifact.
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .context import Context
from .util import ensure_dir, read_text, relpath, sha256_file, write_json


@dataclass(slots=True)
class CacheEntry:
    key: str
    manifest: dict[str, Any]
    directory: Path

    @property
    def outputs(self) -> list[dict[str, Any]]:
        entries = self.manifest.get("outputs")
        return entries if isinstance(entries, list) else []


def cache_root(context: Context, kind: str = "build") -> Path:
    return context.root / ".localci" / "cache" / kind


def cache_key(context: Context, rendered_commands: list[str]) -> str:
    digest = hashlib.sha256()
    digest.update((context.git.revision or "nogit").encode())
    digest.update(b"\x00")
    for command in rendered_commands:
        digest.update(command.encode())
        digest.update(b"\x00")
    return digest.hexdigest()


def lookup(context: Context, key: str, kind: str = "build") -> CacheEntry | None:
    """Return the entry for `key`, or None when it is absent or corrupt.

    Corruption is checked eagerly: every stored output digest is verified
    against the cached bytes before the entry is handed back, so a hit is
    always a restore-able hit.
    """
    directory = cache_root(context, kind) / key
    manifest_path = directory / "cache.json"
    if not manifest_path.is_file():
        return None
    try:
        manifest = _load(manifest_path)
        outputs = manifest["outputs"]
        revision = manifest["revision"]
    except (OSError, ValueError, KeyError):
        return None
    if not isinstance(outputs, list) or not outputs:
        return None
    if revision != (context.git.revision or "nogit"):
        return None
    for entry in outputs:
        if not isinstance(entry, dict):
            return None
        path = directory / "outputs" / str(entry.get("path", ""))
        if not path.is_file() or sha256_file(path) != entry.get("sha256"):
            return None
    return CacheEntry(key=key, manifest=manifest, directory=directory)


def restore(entry: CacheEntry, context: Context) -> list[str]:
    """Copy the cached outputs back into the working tree; returns paths."""
    restored: list[str] = []
    for item in entry.outputs:
        relative = str(item.get("path", ""))
        source = entry.directory / "outputs" / relative
        destination = context.root / relative
        if destination.is_dir():
            shutil.rmtree(destination)
        if source.is_dir():
            shutil.copytree(source, destination, symlinks=True)
        else:
            ensure_dir(destination.parent)
            shutil.copy2(source, destination)
        restored.append(relative)
    return sorted(restored)


def store(
    context: Context,
    key: str,
    rendered_commands: list[str],
    output_paths: list[Path],
    kind: str = "build",
) -> Path | None:
    """Persist a green build's outputs under `key`.

    `output_paths` are working-tree locations (files or directories); each is
    copied preserving its repository-relative path, and every file's digest is
    recorded so `lookup` can detect corruption later.
    """
    if not output_paths:
        return None
    directory = ensure_dir(cache_root(context, kind) / key / "outputs")
    entries: list[dict[str, Any]] = []
    for source in output_paths:
        try:
            relative = source.relative_to(context.root).as_posix()
        except ValueError:
            continue
        destination = directory / relative
        if source.is_dir():
            if destination.exists():
                shutil.rmtree(destination)
            shutil.copytree(source, destination, symlinks=True)
            children = sorted(child for child in destination.rglob("*") if child.is_file())
        else:
            ensure_dir(destination.parent)
            shutil.copy2(source, destination)
            children = [destination]
        entries.extend(
            {
                "path": child.relative_to(directory).as_posix(),
                "sha256": sha256_file(child),
            }
            for child in children
        )
    if not entries:
        return None
    manifest = {
        "schema_version": 1,
        "key": key,
        "revision": context.git.revision or "nogit",
        "short_revision": context.git.short_revision,
        "commands": rendered_commands,
        "created_at": context.started_at.isoformat(),
        "outputs": entries,
    }
    manifest_path = entry_manifest_path(context, key, kind)
    write_json(manifest_path, manifest)
    return manifest_path


def entry_manifest_path(context: Context, key: str, kind: str = "build") -> Path:
    return cache_root(context, kind) / key / "cache.json"


def _load(path: Path) -> dict[str, Any]:
    import json

    document = json.loads(read_text(path))
    if not isinstance(document, dict):
        raise ValueError("cache manifest is not an object")
    return document


def describe(context: Context, key: str) -> str:
    return relpath(entry_manifest_path(context, key).parent, context.root)
