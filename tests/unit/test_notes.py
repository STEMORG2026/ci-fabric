from __future__ import annotations

from pathlib import Path

from ci_fabric.notes import add_note, clear_unreleased, show


def test_add_note_creates_unreleased_section(tmp_path: Path) -> None:
    p = tmp_path / "RELEASE_NOTES.md"
    p.write_text("# Demo 1.0.0\n\nNotes.\n", encoding="utf-8")
    add_note(p, "first note")
    text = p.read_text(encoding="utf-8")
    assert "## Unreleased" in text
    assert "### Added" in text
    assert "- first note" in text


def test_add_note_accumulates(tmp_path: Path) -> None:
    p = tmp_path / "RELEASE_NOTES.md"
    p.write_text("# Demo 1.0.0\n\nNotes.\n", encoding="utf-8")
    add_note(p, "first")
    add_note(p, "second", section="Fixed")
    text = p.read_text(encoding="utf-8")
    assert "- first" in text
    assert "### Fixed" in text
    assert "- second" in text


def test_clear_removes_unreleased(tmp_path: Path) -> None:
    p = tmp_path / "RELEASE_NOTES.md"
    p.write_text("# Demo 1.0.0\n\nNotes.\n", encoding="utf-8")
    add_note(p, "x")
    assert clear_unreleased(p) is True
    text = p.read_text(encoding="utf-8")
    assert "## Unreleased" not in text
    assert clear_unreleased(p) is False


def test_show_missing_file(tmp_path: Path) -> None:
    assert show(tmp_path / "nope.md") == ""
