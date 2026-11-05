"""The release tag gate: a tag may only publish the version it names."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check-release-tag.sh"


def _run(root: Path, tag: str | None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.pop("TAG", None)
    if tag is not None:
        env["TAG"] = tag
    return subprocess.run(
        ["sh", str(SCRIPT), str(root)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_matching_tag_passes(tmp_path: Path) -> None:
    (tmp_path / "VERSION").write_text("0.14.0\n", encoding="utf-8")
    result = _run(tmp_path, "v0.14.0")
    assert result.returncode == 0, result.stderr
    assert "matches VERSION" in result.stdout


def test_mismatched_tag_fails(tmp_path: Path) -> None:
    (tmp_path / "VERSION").write_text("0.14.0\n", encoding="utf-8")
    result = _run(tmp_path, "v0.13.0")
    assert result.returncode == 1
    assert "does not match VERSION" in result.stderr


def test_missing_tag_fails(tmp_path: Path) -> None:
    (tmp_path / "VERSION").write_text("0.14.0\n", encoding="utf-8")
    result = _run(tmp_path, None)
    assert result.returncode == 1
    assert "TAG environment variable is required" in result.stderr


def test_missing_version_file_fails(tmp_path: Path) -> None:
    result = _run(tmp_path, "v0.14.0")
    assert result.returncode == 1
    assert "missing VERSION file" in result.stderr
