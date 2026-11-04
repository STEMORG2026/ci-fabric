"""Changelog structure: release headings are parsed, and repeats are detected.

The repeated heading is the signature of a hand-written prepend that re-emitted
the anchor line, so the parser has to survive real changelogs (Keep a
Changelog `## [1.0.0]`, plain `# name 1.0.0`, fenced examples) without counting
anything that is not a release section.
"""

from __future__ import annotations

from pathlib import Path

from ci_fabric.repository import check_release_notes, release_headings

SAMPLE = """\
# Demo 1.0.0

Notes for 1.0.0.

## [0.9.0] - 2026-01-02

Notes for 0.9.0.

### Internal

- `### Internal` is not a release section.

## Unreleased

No version token here: a changelog may keep a running section.

```toml
# Demo 0.0.1
```

# Demo 0.8.0

Notes for 0.8.0.
"""


def test_release_headings_finds_level_one_and_two_version_headings() -> None:
    headings = release_headings(SAMPLE)
    assert [heading.text for heading in headings] == [
        "# Demo 1.0.0",
        "## [0.9.0] - 2026-01-02",
        "# Demo 0.8.0",
    ]
    assert [heading.line for heading in headings] == [1, 5, 21]


def test_release_headings_ignores_deeper_headings_versions_absent_and_fences() -> None:
    texts = [heading.text for heading in release_headings(SAMPLE)]
    assert "### Internal" not in texts
    assert "## Unreleased" not in texts
    assert "# Demo 0.0.1" not in texts  # inside a fenced example


def test_check_release_notes_reports_a_repeated_heading_with_its_line() -> None:
    # The real defect: prepending a section re-emitted the previous heading.
    text = "# Demo 1.0.0\n\nNew.\n\n# Demo 0.9.0\n\n# Demo 0.9.0\n\nOld.\n"
    _, duplicates = check_release_notes(Path("/nonexistent"), None)
    assert duplicates == []

    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "RELEASE_NOTES.md").write_text(text, encoding="utf-8", newline="\n")
        headings, duplicates = check_release_notes(root, "RELEASE_NOTES.md")
        assert [heading.line for heading in headings] == [1, 5, 7]
        assert [(item.line, item.text) for item in duplicates] == [(7, "# Demo 0.9.0")]


def test_check_release_notes_tolerates_a_missing_file() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        assert check_release_notes(Path(directory), "RELEASE_NOTES.md") == ([], [])
