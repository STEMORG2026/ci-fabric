"""Declared build outputs are cleaned before the build runs.

A release bundle must be a function of the revision, not of the machine's
build history. ``python -m build --outdir dist`` (like most toolchains) leaves
whatever earlier releases put in the output directory, so a build on a clean
tree shipped every historical distribution inside the new bundle and produced
a package digest no fresh clone could reproduce.
"""

from __future__ import annotations

import tarfile
from pathlib import Path

from tests.support import gate, make_repo, run_fixture_pipeline, write_files

BUILD_SECTIONS = (
    "[stages.build]\n"
    "required = true\n"
    'commands = ["mkdir -p dist && echo payload > dist/app.bin"]\n'
    'outputs = ["dist"]'
)

PACKAGE_SECTIONS = (
    '[stages.package]\nrequired = true\nstrategy = "archive"\n'
    'inputs = ["dist"]\nstrip_prefix = "dist"\noutput = "{artifacts}/package.tar.gz"'
)

IGNORE_DIST = ".ci/\n.localci/\ndist/\n"
STALE_NAME = "previous-release.whl"
STALE_CONTENT = "stale distribution from the previous build\n"


def make_repo_with_stale_dist(tmp_path: Path, *, sections: tuple[str, ...]) -> Path:
    files = {
        ".gitignore": IGNORE_DIST,
        # Exactly the residue an appending toolchain leaves behind; ignored, so
        # the tree is still "clean" as far as Git and the cache are concerned.
        f"dist/{STALE_NAME}": STALE_CONTENT,
    }
    return make_repo(tmp_path, files=files, sections=sections)


def test_stale_output_paths_are_removed_before_the_build(tmp_path: Path) -> None:
    root = make_repo_with_stale_dist(tmp_path, sections=(BUILD_SECTIONS,))
    result, _context = run_fixture_pipeline(root, command="build")

    assert not (root / "dist" / STALE_NAME).exists()
    assert (root / "dist" / "app.bin").read_text(encoding="utf-8") == "payload\n"
    clean = gate(result, "build.clean")
    assert clean.status == "PASS"
    assert "removed 1 stale output path" in clean.summary
    # The declared output path (the whole directory) is what was removed.
    assert clean.details["removed"] == ["dist"]
    assert clean.details["declared"] == ["dist"]
    assert gate(result, "build.outputs").status == "PASS"


def test_stale_outputs_never_reach_the_package_archive(tmp_path: Path) -> None:
    root = make_repo_with_stale_dist(tmp_path, sections=(BUILD_SECTIONS, PACKAGE_SECTIONS))
    _result, context = run_fixture_pipeline(root, command="build")

    archive = context.paths.artifacts_dir / "package.tar.gz"
    assert archive.is_file()
    with tarfile.open(archive, "r:gz") as bundle:
        names = sorted(member.name for member in bundle.getmembers() if member.isfile())
    assert names == ["app.bin"], names
    assert not (root / "dist" / STALE_NAME).exists()


def test_cache_hit_does_not_restore_stale_files(tmp_path: Path) -> None:
    sections = (
        "[stages.build]\n"
        "required = true\n"
        "cache = true\n"
        'commands = ["mkdir -p dist && echo payload > dist/app.bin"]\n'
        'outputs = ["dist"]',
    )
    root = make_repo_with_stale_dist(tmp_path, sections=sections)
    run_fixture_pipeline(root, command="build")

    # Residue appears between the cached build and the next run: a hit must
    # restore the cached tree, not merge into whatever is already there.
    write_files(root, {f"dist/{STALE_NAME}": STALE_CONTENT})
    second, _context = run_fixture_pipeline(root, command="build")

    assert "cache hit" in gate(second, "build.cache").summary
    assert not (root / "dist" / STALE_NAME).exists()
    assert (root / "dist" / "app.bin").read_text(encoding="utf-8") == "payload\n"
    assert gate(second, "build.clean").status == "PASS"


def test_checkout_root_is_refused_rather_than_cleaned(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        sections=('[stages.build]\nrequired = true\ncommands = ["true"]\noutputs = ["."]',),
    )
    result, _context = run_fixture_pipeline(root, command="build")

    clean = gate(result, "build.clean")
    assert clean.status == "FAIL"
    assert "is the repository root" in clean.summary
    # Nothing was deleted, and the build command was not run on a polluted tree.
    assert (root / "ci.toml").is_file()
    assert (root / "VERSION").is_file()
    assert all(g.id != "build.01" for g in result.all_gates)
    assert result.exit_code == 1


def test_tracked_paths_are_never_deleted(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        sections=(
            '[stages.build]\nrequired = true\ncommands = ["true"]\noutputs = ["VERSION", "dist"]'
        ),
    )
    result, _context = run_fixture_pipeline(root, command="build")

    # A tracked file is not a build product: it is reported, never removed.
    assert (root / "VERSION").is_file()
    assert gate(result, "build.clean.tracked").status == "WARN"
    assert "VERSION" in gate(result, "build.clean.tracked").summary
