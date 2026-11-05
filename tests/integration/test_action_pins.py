"""The action-pin gate: external `uses:` must be full commit SHAs.

SHA pinning is only half the discipline (the other half is Dependabot
keeping the pins current); this gate fails the shape that silently rots —
a mutable tag or branch in a workflow.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check-action-pins.sh"


def _run(directory: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(SCRIPT), str(directory)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def test_sha_pins_and_local_actions_pass(tmp_path: Path) -> None:
    (tmp_path / "ci.yml").write_text(
        "jobs:\n"
        "  ci:\n"
        "    steps:\n"
        "      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1\n"
        "      - uses: ./local-action\n",
        encoding="utf-8",
    )
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr


def test_a_mutable_tag_fails(tmp_path: Path) -> None:
    (tmp_path / "ci.yml").write_text(
        "jobs:\n  ci:\n    steps:\n      - uses: actions/checkout@v7\n",
        encoding="utf-8",
    )
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "actions/checkout@v7" in result.stderr


def test_a_short_sha_fails(tmp_path: Path) -> None:
    (tmp_path / "ci.yml").write_text(
        "jobs:\n  ci:\n    steps:\n      - uses: actions/checkout@3d3c42e5\n",
        encoding="utf-8",
    )
    result = _run(tmp_path)
    assert result.returncode == 1


def test_the_repositorys_own_workflows_are_pinned() -> None:
    result = subprocess.run(
        ["sh", str(SCRIPT)], capture_output=True, text=True, check=False, timeout=30
    )
    assert result.returncode == 0, result.stderr
