"""Content-addressed build cache: same clean tree, same commands, no rebuild.

The key is (git revision, rendered build commands) — on a clean tree the
revision pins every tracked byte, so a hit is honest. Dirty trees are never
looked up and never stored; only green builds are cached; restores are
digest-verified, so corruption is a miss, never a wrong artifact.
"""

from __future__ import annotations

import json
from pathlib import Path

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
