"""Tier-measured coverage: the tiers measure, the coverage stage combines.

Declaring `measure_coverage = true` on a test tier is the whole configuration:
the tier gets its own parallel data file inside the run's artifact directory and
its command runs under `coverage run --parallel-mode`, and a coverage stage
without a command turns whatever the tiers measured into the report the
threshold is judged on. Before this, every project hand-wired the environment
variable, the `coverage run` prefix, the combine step and the XML path.
"""

from __future__ import annotations

from pathlib import Path

from tests.support import gate, make_repo, run_fixture_pipeline

CALC = "def add(a, b):\n    return a + b\n"
TEST_CALC = "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"

MEASURED_UNIT = (
    "[stages.unit]\n"
    "required = true\n"
    "measure_coverage = true\n"
    'commands = ["{python} -m pytest -q tests --junitxml={artifacts}/unit.xml"]\n'
    'junit_path = "{artifacts}/unit.xml"\n'
    'test_globs = ["tests/**/*.py"]\n'
    "min_test_files = 1"
)

COMBINE_STAGE = "[stages.coverage]\nrequired = true\nmin_percent = 1"

MEASURED_FILES = {"tests/calc.py": CALC, "tests/test_calc.py": TEST_CALC}


def test_tier_measurement_is_wired_and_combined(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        files=MEASURED_FILES,
        sections=(MEASURED_UNIT, COMBINE_STAGE),
    )
    result, context = run_fixture_pipeline(root, command="test")

    # The tier ran under coverage without the consumer writing it.
    command_gate = gate(result, "unit.01")
    assert command_gate.status == "PASS", command_gate.summary
    assert "-m coverage run --parallel-mode -m pytest" in (command_gate.command or "")
    # ... and the measurement is its own recorded fact.
    measured = gate(result, "unit.coverage")
    assert measured.status == "PASS"
    assert measured.details["mode"] == "wrapped"
    assert measured.details["data_file"].endswith("coverage.data")
    # The tier's parallel file went into this run's artifact directory and the
    # combine step wrote the combined file next to it; nothing landed in the
    # repository root.
    assert (context.paths.artifacts_dir / "coverage.data").is_file()
    assert not (root / "coverage.data").exists()

    # The coverage stage had no command and still combined, reported and wrote
    # the XML the threshold is judged on.
    combine = gate(result, "coverage.command")
    assert combine.status == "PASS"
    assert "coverage combine" in (combine.command or "")
    assert (context.paths.artifacts_dir / "coverage.xml").is_file()
    threshold = gate(result, "coverage.threshold")
    assert threshold.status == "PASS"
    assert threshold.details["source"] == "line-rate"
    assert threshold.details["percent"] > 0
    assert result.exit_code == 0


def test_a_command_that_cannot_be_measured_fails_instead_of_passing_unmeasured(
    tmp_path: Path,
) -> None:
    root = make_repo(
        tmp_path,
        files=MEASURED_FILES,
        sections=(
            "[stages.unit]\n"
            "required = true\n"
            "measure_coverage = true\n"
            'commands = ["make test"]\n'
            'test_globs = ["tests/**/*.py"]'
        ),
    )
    result, _context = run_fixture_pipeline(root, command="unit")

    measured = gate(result, "unit.coverage")
    assert measured.status == "FAIL"
    assert "make test" in measured.summary
    assert "coverage run" in (measured.remediation or "")
    # The unmeasured command must not run and produce a green tier.
    assert all(g.id != "unit.01" for g in result.all_gates)
    assert result.exit_code == 1


def test_measuring_without_a_coverage_stage_is_reported(tmp_path: Path) -> None:
    root = make_repo(tmp_path, files=MEASURED_FILES, sections=(MEASURED_UNIT,))
    result, _context = run_fixture_pipeline(root, command="unit")

    measured = gate(result, "unit.coverage")
    assert measured.status == "WARN"
    assert "coverage stage" in measured.summary
    # A warning is not a failure: the tier itself is still verified.
    assert result.exit_code == 0


def test_standalone_coverage_without_measurement_fails_with_guidance(
    tmp_path: Path,
) -> None:
    # The loader accepts this (a tier does declare measure_coverage), but this
    # `./ci coverage` run measured nothing: no data to combine and no command to
    # run. The stage must say so instead of reporting an unverified pass.
    root = make_repo(
        tmp_path,
        files=MEASURED_FILES,
        sections=(MEASURED_UNIT, "[stages.coverage]\nrequired = true"),
    )
    result, _context = run_fixture_pipeline(root, command="coverage")

    config_gate = gate(result, "coverage.config")
    assert config_gate.status == "FAIL"
    assert "no test tier measured coverage in this run" in config_gate.summary
    assert "measure_coverage" in (config_gate.remediation or "")
    assert result.exit_code == 1


def test_a_configured_command_still_runs_a_standalone_coverage_check(tmp_path: Path) -> None:
    # No tier measured anything in this run: the configured command is the
    # standalone path and must not be replaced by the combine sequence.
    root = make_repo(
        tmp_path,
        files={
            "write_cov.py": "import sys\nfrom pathlib import Path\n\n"
            "Path(sys.argv[1]).write_text('<coverage line-rate=\"0.9\"></coverage>')\n"
        },
        sections=(
            "[stages.coverage]\n"
            "required = true\n"
            'commands = ["{python} write_cov.py {artifacts}/coverage.xml"]\n'
            'report_path = "{artifacts}/coverage.xml"\n'
            "min_percent = 50"
        ),
    )
    result, _context = run_fixture_pipeline(root, command="coverage")

    command_gate = gate(result, "coverage.command")
    assert "write_cov.py" in (command_gate.command or "")
    assert "combine" not in (command_gate.command or "")
    assert gate(result, "coverage.threshold").status == "PASS"
    assert result.exit_code == 0
