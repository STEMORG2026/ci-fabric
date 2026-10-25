"""Dependency-lockfile parsers feeding the CycloneDX SBOM."""

from __future__ import annotations

from pathlib import Path

from ci_fabric.supplychain import collect_components, find_lockfiles
from tests.support import make_repo


def test_uv_and_poetry_lock_parsers(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        files={
            "uv.lock": (
                'version = 1\n\n[[package]]\nname = "flask"\nversion = "3.0.0"\n\n'
                '[[package]]\nname = "jinja2"\nversion = "3.1.4"\n'
            ),
        },
    )
    components, warnings = collect_components(root)
    assert warnings == []
    found = {(c.name, c.version, c.ecosystem) for c in components}
    assert ("flask", "3.0.0", "pypi") in found
    assert ("jinja2", "3.1.4", "pypi") in found


def test_poetry_lock_parser(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        files={
            "poetry.lock": (
                '[[package]]\nname = "requests"\nversion = "2.32.0"\n\n'
                '[[package]]\nname = "no-version"\n'
            ),
        },
    )
    components, _warnings = collect_components(root)
    found = {(c.name, c.version) for c in components}
    assert ("requests", "2.32.0") in found
    assert all(name != "no-version" for name, _ in found)


def test_npm_package_lock_parser(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        files={
            "package-lock.json": (
                "{\n"
                '  "packages": {\n'
                '    "node_modules/lodash": {"name": "lodash", "version": "4.17.21"},\n'
                '    "node_modules/unnamed": {"version": "9.9.9"},\n'
                '    "": {"name": "self", "version": "1.0.0"}\n'
                "  }\n"
                "}"
            ),
        },
    )
    components, _warnings = collect_components(root)
    found = {(c.name, c.version, c.ecosystem) for c in components}
    assert ("lodash", "4.17.21", "npm") in found
    # The root package entry (empty location) is a project, not a dependency.
    assert ("self", "1.0.0") not in {(name, version) for name, version, _ in found}


def test_pnpm_lock_parser(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        files={
            "pnpm-lock.yaml": (
                "lockfileVersion: '9.0'\n"
                "importers:\n  .: {}\n"
                "packages:\n"
                "  react@18.3.1:\n"
                "    resolution: {integrity: sha512-abc}\n"
            ),
        },
    )
    components, _warnings = collect_components(root)
    assert ("react", "18.3.1", "npm") in {(c.name, c.version, c.ecosystem) for c in components}


def test_cargo_and_go_lock_parsers(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        files={
            "Cargo.lock": (
                '[[package]]\nname = "serde"\nversion = "1.0.200"\n\n'
                '[[package]]\nname = "tokio"\nversion = "1.38.0"\n'
            ),
            "go.sum": "github.com/stretchr/testify v1.9.0 h1:abc=\n",
        },
    )
    components, _warnings = collect_components(root)
    found = {(c.name, c.version, c.ecosystem) for c in components}
    assert ("serde", "1.0.200", "cargo") in found
    assert ("tokio", "1.38.0", "cargo") in found
    assert ("github.com/stretchr/testify", "v1.9.0", "golang") in found


def test_malformed_lockfiles_report_warnings_not_crashes(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        files={
            "package-lock.json": "{not json",
            "poetry.lock": "not = [valid\n",
        },
    )
    components, warnings = collect_components(root)
    assert components == []
    assert len(warnings) == 2
    assert any("package-lock.json" in warning for warning in warnings)
    assert any("poetry.lock" in warning for warning in warnings)


def test_requirements_lock_files_feed_the_sbom(tmp_path: Path) -> None:
    # Hash-pinned pip-format lockfiles (pip-tools/uv compiled) are as good a
    # dependency source as requirements.txt and must not be invisible to the SBOM.
    root = make_repo(
        tmp_path,
        files={
            "requirements-dev.lock": (
                "pytest==9.1.1 --hash=sha256:" + "a" * 64 + "\n"
                "ruff==0.9.4 --hash=sha256:" + "b" * 64 + "\n"
            ),
        },
    )
    components, warnings = collect_components(root)
    assert warnings == []
    found = {(c.name, c.version, c.ecosystem) for c in components}
    assert ("pytest", "9.1.1", "pypi") in found
    assert ("ruff", "0.9.4", "pypi") in found
    assert any(p.name == "requirements-dev.lock" for p in find_lockfiles(root))


def test_find_lockfiles_skips_venv_directories(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        files={
            ".venv/requirements.txt": "inside==1.0\n",
            "sub/requirements.txt": "nested==2.0\n",
        },
    )
    found = list(find_lockfiles(root))
    names = [str(path) for path in found]
    assert any("sub/requirements.txt" in name for name in names)
    assert not any(".venv" in name for name in names)
