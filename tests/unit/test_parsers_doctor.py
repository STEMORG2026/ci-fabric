"""Unit coverage for helpers added during test-hardening: glob_match and parsers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ci_fabric.stages.coverage import _coverage_from_output, parse_coverage_report
from ci_fabric.util import glob_match, matches_any


@pytest.mark.parametrize(
    ("relative", "pattern", "expected"),
    (
        # recursive ** spans zero or more directories
        ("tests/test_x.py", "tests/**/*.py", True),
        ("tests/a/b/c/test_x.py", "tests/**/*.py", True),
        ("tests/test_x.py", "tests/*.py", True),
        ("other/test_x.py", "tests/*.py", False),
        ("test_x.py", "**/test_*.py", True),
        ("pkg/sub/test_x.py", "**/test_*.py", True),
        # * never crosses a separator
        ("a/b.py", "*.py", False),
        ("b.py", "*.py", True),
        # directory prefixes and trailing /**
        ("node_modules/x/y", "node_modules/**", True),
        ("docs/guide.md", "docs/**/*.md", True),
        ("docs/a/b/guide.md", "docs/**/*.md", True),
        # exact matches and single-segment wildcards
        (".env", ".env", True),
        (".env.local", ".env.*", True),
        ("es.txt", "*.txt", True),
    ),
)
def test_glob_match_semantics(relative: str, pattern: str, expected: bool) -> None:
    assert glob_match(relative, pattern) is expected


def test_matches_any_helper() -> None:
    assert matches_any("a/b.py", ["x", "**/*.py"]) is True
    assert matches_any("a/b.py", ["x", "y"]) is False


def test_parse_coverage_report_formats(tmp_path: Path) -> None:
    xml = tmp_path / "coverage.xml"
    xml.write_text('<coverage line-rate="0.5"></coverage>', encoding="utf-8")
    assert parse_coverage_report(xml) == (50.0, "line-rate")

    report_json = tmp_path / "coverage.json"
    report_json.write_text(json.dumps({"totals": {"percent_covered": 81.5}}), encoding="utf-8")
    assert parse_coverage_report(report_json) == (81.5, "percent_covered")

    flat_json = tmp_path / "flat.json"
    flat_json.write_text(json.dumps({"percent_covered": 42}), encoding="utf-8")
    assert parse_coverage_report(flat_json) == (42.0, "percent_covered")

    string_json = tmp_path / "string.json"
    string_json.write_text(json.dumps({"percent_covered": "42"}), encoding="utf-8")
    # Non-numeric totals are rejected rather than misread.
    assert parse_coverage_report(string_json) == (None, "JSON has no coverage total")

    lcov = tmp_path / "coverage.info"
    lcov.write_text("LF:10\nLH:4\nLF:10\nLH:6\n", encoding="utf-8")
    assert parse_coverage_report(lcov) == (50.0, "lcov lines")

    empty_lcov = tmp_path / "empty.info"
    empty_lcov.write_text("LF:0\nLH:0\n", encoding="utf-8")
    assert parse_coverage_report(empty_lcov) == (None, "lcov has no line data")

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    percent, reason = parse_coverage_report(broken)
    assert percent is None
    assert "could not parse" in reason

    unsupported = tmp_path / "coverage.txt"
    unsupported.write_text("TOTAL 99%", encoding="utf-8")
    assert parse_coverage_report(unsupported) == (None, "unsupported coverage report format")


def test_coverage_from_output_patterns() -> None:
    assert _coverage_from_output("TOTAL    1500    300   83.3%") == (83.3, "TOTAL line")
    # A table line must yield the coverage column, not the first statement count.
    assert _coverage_from_output("All files | 100 | 20 | 80") == (80.0, "All files line")
    assert _coverage_from_output("All files | 100 | 0 | 100 |") == (100.0, "All files line")
    assert _coverage_from_output("no numbers here") == (None, "no coverage percentage in output")


def test_doctor_report_shape_and_print(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from ci_fabric.config import load_config
    from ci_fabric.context import create_context
    from ci_fabric.doctor import diagnose, print_report
    from tests.support import make_repo

    root = make_repo(tmp_path, sections=('[stages.lint]\nrequired = true\ncommands = ["true"]',))
    context = create_context(root, load_config(root))
    report = diagnose(context)

    assert report.checks, "doctor must produce checks"
    document = report.to_dict()
    assert {"ok", "started_at", "checks", "failures", "warnings"} == set(document)
    for check in report.checks:
        assert check.name
        assert isinstance(check.ok, bool)
        assert check.detail
    # A healthy fixture must not report required-check failures.
    assert report.ok is True

    print_report(report)
    captured = capsys.readouterr().out
    assert "doctor" in captured.lower()
