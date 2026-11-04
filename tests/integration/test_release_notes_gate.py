"""The repository stage refuses a changelog that lists a release twice.

A changelog is checksummed into every release bundle, so a duplicated section
is a defect in the shipped artifact — and it is exactly what a hand-written
prepend produces when it re-emits the previous heading as its anchor. Five of
this repository's own releases shipped that way before the gate existed.
"""

from __future__ import annotations

from pathlib import Path

from tests.support import gate, make_repo, run_fixture_pipeline

RELEASE_SECTION = 'changelog = "RELEASE_NOTES.md"\nrequire_notes = true'

CLEAN_NOTES = (
    "# Demo 1.0.0\n\nInitial release.\n\n"
    "# Demo 0.9.0\n\nPrevious release.\n\n"
    "# Demo 0.8.0\n\nOlder release.\n"
)

# The exact shape the defect took: the new section, then the previous heading
# written twice in a row.
DUPLICATED_NOTES = (
    "# Demo 1.0.0\n\nInitial release.\n\n"
    "# Demo 0.9.0\n\n# Demo 0.9.0\n\nPrevious release.\n\n"
    "# Demo 0.8.0\n\nOlder release.\n"
)


def test_repeated_release_heading_fails_the_gate(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        files={"RELEASE_NOTES.md": DUPLICATED_NOTES},
        release=RELEASE_SECTION,
    )
    result, _context = run_fixture_pipeline(root, command="validate")

    notes = gate(result, "repository.release-notes")
    assert notes.status == "FAIL"
    assert "Demo 0.9.0" in notes.summary
    assert "line 7" in notes.summary
    assert [item["line"] for item in notes.details["duplicates"]] == [7]
    assert result.exit_code == 1


def test_a_clean_changelog_passes(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        files={"RELEASE_NOTES.md": CLEAN_NOTES},
        release=RELEASE_SECTION,
    )
    result, _context = run_fixture_pipeline(root, command="validate")

    notes = gate(result, "repository.release-notes")
    assert notes.status == "PASS"
    assert "3 release section(s)" in notes.summary
    assert notes.details["newest"] == "# Demo 1.0.0"


def test_keep_a_changelog_style_headings_are_checked_too(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        files={
            "RELEASE_NOTES.md": (
                "## [1.0.0] - 2026-02-01\n\nNew.\n\n"
                "## [0.9.0] - 2026-01-01\n\n## [0.9.0] - 2026-01-01\n\nOld.\n"
            )
        },
        release=RELEASE_SECTION,
    )
    result, _context = run_fixture_pipeline(root, command="validate")

    notes = gate(result, "repository.release-notes")
    assert notes.status == "FAIL"
    assert "line 7 repeats ## [0.9.0] - 2026-01-01" in notes.summary


def test_missing_changelog_is_skipped_not_warned(tmp_path: Path) -> None:
    # Default [release] config points at CHANGELOG.md, which this fixture lacks.
    # Existence is the release stage's business: a project without notes yet is
    # not warned on every run.
    root = make_repo(tmp_path)
    result, _context = run_fixture_pipeline(root, command="validate")

    notes = gate(result, "repository.release-notes")
    assert notes.status == "SKIP"
    assert "CHANGELOG.md" in notes.summary
    assert result.exit_code == 0


def test_changelog_structure_is_checked_when_notes_are_not_required(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        files={"RELEASE_NOTES.md": DUPLICATED_NOTES},
        release='changelog = "RELEASE_NOTES.md"\nrequire_notes = false',
    )
    result, _context = run_fixture_pipeline(root, command="validate")

    # Structure is an invariant of the file, not of the release policy.
    assert gate(result, "repository.release-notes").status == "FAIL"
