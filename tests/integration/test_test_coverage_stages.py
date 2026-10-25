"""Test stages and coverage: discovery, zero-test detection, JUnit, thresholds."""

from __future__ import annotations

from pathlib import Path

from tests.support import gate, make_repo, run_fixture_pipeline, stage

WRITE_COVERAGE_XML = """\
import sys
from pathlib import Path

Path(sys.argv[1]).write_text('<coverage line-rate="0.5"></coverage>')
"""


def test_discovery_gate_blocks_a_command_without_tests(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        sections=(
            '[stages.unit]\nrequired = true\ncommands = ["true"]\ntest_globs = ["tests/**/*.py"]',
        ),
    )
    result, _context = run_fixture_pipeline(root, command="unit")
    unit = stage(result, "unit")
    assert unit.status == "FAIL"
    discovery = gate(result, "unit.discovery")
    assert discovery.status == "FAIL"
    assert discovery.classification == "BLOCKING"
    assert "minimum is 1" in discovery.summary
    assert "silently run zero tests" in discovery.summary
    assert discovery.details["minimum"] == 1
    assert discovery.details["files"] == []
    # The configured command must not run once discovery fails.
    assert [g.id for g in result.all_gates if g.command] == []
    assert result.exit_code == 1


def test_zero_test_output_is_detected_even_on_exit_zero(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        files={"tests/test_ok.py": "def test_ok():\n    assert True\n"},
        sections=(
            "[stages.unit]\n"
            "required = true\n"
            "commands = [\"echo 'collected 0 items'\"]\n"
            'test_globs = ["tests/**/*.py"]'
        ),
    )
    result, _context = run_fixture_pipeline(root, command="unit")
    command_gate = gate(result, "unit.01")
    assert command_gate.status == "FAIL"
    assert "zero tests" in command_gate.summary
    assert stage(result, "unit").status == "FAIL"


def test_unit_stage_runs_pytest_and_parses_junit(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        files={"tests/test_ok.py": "def test_ok():\n    assert 1 + 1 == 2\n"},
        sections=(
            "[stages.unit]\n"
            "required = true\n"
            'commands = ["{python} -m pytest -q tests --junitxml={artifacts}/unit.xml"]\n'
            'junit_path = "{artifacts}/unit.xml"\n'
            'test_globs = ["tests/**/*.py"]\n'
            "min_tests = 1"
        ),
    )
    result, _context = run_fixture_pipeline(root, command="unit")
    assert stage(result, "unit").status == "PASS", [g.summary for g in result.all_gates]
    discovery = gate(result, "unit.discovery")
    assert discovery.details["files"] == ["tests/test_ok.py"]
    junit = gate(result, "unit.junit")
    assert junit.status == "PASS"
    assert junit.details == {
        "tests": 1,
        "failures": 0,
        "errors": 0,
        "skipped": 0,
        "suites": 1,
    }
    assert junit.artifacts == [junit.log and junit.artifacts[0]][0:1] or junit.artifacts
    assert (root / junit.artifacts[0]).is_file()


def test_junit_minimum_is_enforced_from_reported_counts(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        files={"tests/test_ok.py": "def test_ok():\n    assert True\n"},
        sections=(
            "[stages.unit]\n"
            "required = true\n"
            'commands = ["{python} -m pytest -q tests --junitxml={artifacts}/unit.xml"]\n'
            'junit_path = "{artifacts}/unit.xml"\n'
            'test_globs = ["tests/**/*.py"]\n'
            "min_tests = 5"
        ),
    )
    result, _context = run_fixture_pipeline(root, command="unit")
    junit = gate(result, "unit.junit")
    assert junit.status == "FAIL"
    assert "1 test(s) reported, minimum is 5" in junit.summary
    assert junit.details["tests"] == 1


def test_coverage_threshold_reads_xml_and_fails_below_minimum(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        files={"write_cov.py": WRITE_COVERAGE_XML},
        sections=(
            "[stages.coverage]\n"
            "required = true\n"
            'commands = ["{python} write_cov.py {artifacts}/coverage.xml"]\n'
            'report_path = "{artifacts}/coverage.xml"\n'
            "min_percent = 80"
        ),
    )
    result, _context = run_fixture_pipeline(root, command="coverage")
    threshold = gate(result, "coverage.threshold")
    assert threshold.status == "FAIL"
    assert threshold.blocking_failure is True
    assert "50.00% < 80.00% (line-rate)" in threshold.summary
    assert threshold.details["percent"] == 50.0
    assert threshold.remediation and "lower the threshold" in threshold.remediation
    assert result.exit_code == 1


def test_coverage_threshold_passes_at_or_above_minimum(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        files={"write_cov.py": WRITE_COVERAGE_XML},
        sections=(
            "[stages.coverage]\n"
            "required = true\n"
            'commands = ["{python} write_cov.py {artifacts}/coverage.xml"]\n'
            'report_path = "{artifacts}/coverage.xml"\n'
            "min_percent = 50"
        ),
    )
    result, _context = run_fixture_pipeline(root, command="coverage")
    threshold = gate(result, "coverage.threshold")
    assert threshold.status == "PASS"
    assert "50.00% (line-rate)" in threshold.summary
    assert result.exit_code == 0


def test_coverage_without_threshold_reports_but_never_blocks(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        files={"write_cov.py": WRITE_COVERAGE_XML},
        sections=(
            "[stages.coverage]\n"
            "required = true\n"
            'commands = ["{python} write_cov.py {artifacts}/coverage.xml"]\n'
            'report_path = "{artifacts}/coverage.xml"'
        ),
    )
    result, _context = run_fixture_pipeline(root, command="coverage")
    report_gate = gate(result, "coverage.report")
    assert report_gate.status == "PASS"
    assert report_gate.details["percent"] == 50.0
    assert result.exit_code == 0
