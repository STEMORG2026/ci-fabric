"""Content-addressed build cache: same clean tree, same commands, no rebuild.

The key is (git revision, rendered build commands) — on a clean tree the
revision pins every tracked byte, so a hit is honest. Dirty trees are never
looked up and never stored; only green builds are cached; restores are
digest-verified, so corruption is a miss, never a wrong artifact.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ci_fabric import buildcache
from ci_fabric.errors import CiError
from tests.support import commit_all, gate, make_repo, run_fixture_pipeline, write_files

CACHE_SECTIONS = (
    "[stages.build]\n"
    "required = true\n"
    "cache = true\n"
    "commands = [\"bash -c 'echo built >> build-marker.txt; mkdir -p dist; "
    "echo payload > dist/app.bin'\"]\n"
    'outputs = ["dist"]',
)


def make_cached_repo(tmp_path: Path, **kwargs: object) -> Path:
    files = {".gitignore": ".ci/\n.localci/\nbuild-marker.txt\n"}
    return make_repo(tmp_path, files=files, sections=CACHE_SECTIONS, **kwargs)  # type: ignore[arg-type]


def test_first_run_is_a_miss_that_stores(tmp_path: Path) -> None:
    root = make_cached_repo(tmp_path)
    result, _context = run_fixture_pipeline(root, command="build")
    cache_gate = gate(result, "build.cache")
    assert cache_gate.status == "PASS"
    assert "cached under" in cache_gate.summary
    assert (root / "build-marker.txt").read_text(encoding="utf-8").count("built") == 1
    entries = list((root / ".localci" / "cache" / "build").iterdir())
    assert len(entries) == 1
    manifest = json.loads((entries[0] / "cache.json").read_text(encoding="utf-8"))
    assert manifest["revision"]


def test_second_run_at_same_revision_is_a_hit_and_skips_the_build(tmp_path: Path) -> None:
    root = make_cached_repo(tmp_path)
    run_fixture_pipeline(root, command="build")
    marker = (root / "build-marker.txt").read_text(encoding="utf-8")
    assert marker.count("built") == 1

    # Delete the outputs: the cache must restore them without rebuilding.
    import shutil

    shutil.rmtree(root / "dist")
    second, _context = run_fixture_pipeline(root, command="build")
    cache_gate = gate(second, "build.cache")
    assert cache_gate.status == "PASS"
    assert "cache hit" in cache_gate.summary
    assert "build commands skipped" in cache_gate.summary
    # The build command did not run again.
    assert (root / "build-marker.txt").read_text(encoding="utf-8") == marker
    # The restored outputs are byte-identical and pass the outputs gate.
    assert (root / "dist" / "app.bin").read_text(encoding="utf-8") == "payload\n"
    assert gate(second, "build.outputs").status == "PASS"
    outputs_gate = gate(second, "build.outputs")
    assert "restored from cache" in outputs_gate.summary
    metadata = gate(second, "build.metadata")
    assert "cache hit" in metadata.summary


def test_new_commit_invalidates_the_cache(tmp_path: Path) -> None:
    root = make_cached_repo(tmp_path)
    run_fixture_pipeline(root, command="build")
    write_files(root, {"source.txt": "changed\n"})
    commit_all(root, "change the tree")
    second, _context = run_fixture_pipeline(root, command="build")
    cache_gate = gate(second, "build.cache")
    assert "cached under" in cache_gate.summary  # a fresh miss that re-stores
    assert (root / "build-marker.txt").read_text(encoding="utf-8").count("built") == 2


def test_dirty_tree_is_never_consulted_nor_stored(tmp_path: Path) -> None:
    root = make_cached_repo(tmp_path)
    run_fixture_pipeline(root, command="build")
    (root / "uncommitted.txt").write_text("dirty\n", encoding="utf-8")
    second, _context = run_fixture_pipeline(root, command="build")
    cache_gate = gate(second, "build.cache")
    assert cache_gate.status == "WARN"
    assert "tree is dirty" in cache_gate.summary
    assert (root / "build-marker.txt").read_text(encoding="utf-8").count("built") == 2


def test_corrupted_cache_entry_is_a_miss_not_a_wrong_artifact(tmp_path: Path) -> None:
    root = make_cached_repo(tmp_path)
    run_fixture_pipeline(root, command="build")
    entry = next((root / ".localci" / "cache" / "build").iterdir())
    payload = entry / "outputs" / "dist" / "app.bin"
    payload.write_text("tampered\n", encoding="utf-8")

    import shutil

    shutil.rmtree(root / "dist")
    second, _context = run_fixture_pipeline(root, command="build")
    cache_gate = gate(second, "build.cache")
    # Digest verification failed: rebuilt and re-stored, marker ran again.
    assert "cached under" in cache_gate.summary
    assert (root / "build-marker.txt").read_text(encoding="utf-8").count("built") == 2
    assert (root / "dist" / "app.bin").read_text(encoding="utf-8") == "payload\n"


def test_cache_is_opt_in(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        sections=(
            "[stages.build]\n"
            "required = true\n"
            'commands = ["mkdir -p dist && echo x > dist/app.bin"]\n'
            'outputs = ["dist"]',
        ),
    )
    result, _context = run_fixture_pipeline(root, command="build")
    assert all(g.id != "build.cache" for g in result.all_gates)
    assert not (root / ".localci" / "cache").exists()


def _planted_entry(root: Path, outputs: list[dict[str, str]], revision: str = "deadbeef") -> str:
    """Write an attacker-shaped cache manifest; return its key.

    The payload files are placed where the manifest's source resolution
    lands, with matching digests, so only path validation can reject them.
    """
    context = SimpleNamespace(root=root, git=SimpleNamespace(revision=revision))
    key = "k" * 64
    entry_dir = buildcache.cache_root(context) / key
    (entry_dir / "outputs").mkdir(parents=True)
    for item in outputs:
        source = entry_dir / "outputs" / item["path"]
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("ATTACKER CONTENT\n", encoding="utf-8")
        item["sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest = {"schema_version": 1, "key": key, "revision": revision, "outputs": outputs}
    (entry_dir / "cache.json").write_text(json.dumps(manifest), encoding="utf-8")
    return key


def test_a_traversal_manifest_is_a_miss_and_writes_nothing(tmp_path: Path) -> None:
    # Audit CF-03 proof-of-concept: `../victim.txt` resolves to an existing,
    # digest-matching source inside the cache tree, but the restore
    # destination escapes the repository root. Lookup must reject it.
    root = make_cached_repo(tmp_path)
    victim = tmp_path / "victim.txt"
    victim.write_text("ORIGINAL\n", encoding="utf-8")
    key = _planted_entry(root, [{"path": "../victim.txt"}])
    context = SimpleNamespace(root=root, git=SimpleNamespace(revision="deadbeef"))
    assert buildcache.lookup(context, key) is None
    assert victim.read_text(encoding="utf-8") == "ORIGINAL\n"


def test_an_absolute_manifest_path_is_a_miss(tmp_path: Path) -> None:
    # The payload exists and its digest matches; only the absolute path
    # shape can reject this entry.
    root = make_cached_repo(tmp_path)
    context = SimpleNamespace(root=root, git=SimpleNamespace(revision="deadbeef"))
    key = "k" * 64
    entry_dir = buildcache.cache_root(context) / key
    outputs_dir = entry_dir / "outputs"
    outputs_dir.mkdir(parents=True)
    payload = outputs_dir / "payload.txt"
    payload.write_text("ATTACKER CONTENT\n", encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "key": key,
        "revision": "deadbeef",
        "outputs": [
            {"path": str(payload), "sha256": hashlib.sha256(payload.read_bytes()).hexdigest()}
        ],
    }
    (entry_dir / "cache.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert buildcache.lookup(context, key) is None


def test_a_manifest_targeting_a_tracked_file_is_a_miss(tmp_path: Path) -> None:
    # A committed file is a source, never a build product (the build.clean
    # doctrine): the cache must neither serve nor restore over tracked paths.
    root = make_cached_repo(tmp_path)
    before = (root / "VERSION").read_text(encoding="utf-8")
    key = _planted_entry(root, [{"path": "VERSION"}])
    context = SimpleNamespace(root=root, git=SimpleNamespace(revision="deadbeef"))
    assert buildcache.lookup(context, key) is None
    assert (root / "VERSION").read_text(encoding="utf-8") == before


def test_restore_revalidates_even_when_lookup_is_bypassed(tmp_path: Path) -> None:
    # Defense in depth: restore() is public, so it re-checks containment and
    # tracked paths itself rather than trusting that lookup filtered first.
    root = make_cached_repo(tmp_path)
    before = (root / "VERSION").read_text(encoding="utf-8")
    key = _planted_entry(root, [{"path": "VERSION"}])
    context = SimpleNamespace(root=root, git=SimpleNamespace(revision="deadbeef"))
    entry_dir = buildcache.cache_root(context) / key
    manifest = json.loads((entry_dir / "cache.json").read_text(encoding="utf-8"))
    entry = buildcache.CacheEntry(key=key, manifest=manifest, directory=entry_dir)
    with pytest.raises(CiError):
        buildcache.restore(entry, context)
    assert (root / "VERSION").read_text(encoding="utf-8") == before
