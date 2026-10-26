"""ci-fabric: local-first CI/CD infrastructure for any repository."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


def _resolve_version() -> str:
    # A dev checkout declares its version in the VERSION file (the
    # repository.versions gate keeps it in sync with pyproject); an installed
    # distribution is authoritative through its own metadata.
    version_file = Path(__file__).resolve().parents[2] / "VERSION"
    if version_file.is_file():
        declared = version_file.read_text(encoding="utf-8").strip()
        if declared:
            return declared
    try:
        return version("ci-fabric")
    except PackageNotFoundError:  # pragma: no cover - only outside an install
        return "0.0.0"


__version__ = _resolve_version()

__all__ = ["__version__"]
