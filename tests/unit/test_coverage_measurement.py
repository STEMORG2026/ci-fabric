"""Command planning for tier-level coverage measurement.

`measure_coverage = true` has to turn a plain test command into a measured one
without guessing: the canonical shape is `<interpreter> -m <runner> <args>`,
which coverage can wrap losslessly. Anything else (a Makefile target, a shell
one-liner, a script path) is reported as unsupported rather than quietly
measured wrong or not at all.
"""

from __future__ import annotations

from ci_fabric.stages.tests import plan_coverage_invocation


def test_module_invocation_is_wrapped_and_keeps_its_arguments() -> None:
    plan = plan_coverage_invocation(
        "/repo/.venv/bin/python -m pytest -q tests/unit --junitxml={artifacts}/unit.xml"
    )
    assert plan.mode == "wrapped"
    assert plan.command == (
        "/repo/.venv/bin/python -m coverage run --parallel-mode -m pytest -q tests/unit "
        "--junitxml={artifacts}/unit.xml"
    )


def test_quoted_arguments_survive_untouched() -> None:
    plan = plan_coverage_invocation("/usr/bin/python3 -m pytest -q tests -k 'not slow'")
    assert plan.mode == "wrapped"
    assert plan.command == (
        "/usr/bin/python3 -m coverage run --parallel-mode -m pytest -q tests -k 'not slow'"
    )


def test_a_command_that_already_measures_is_left_alone() -> None:
    plan = plan_coverage_invocation(
        "/repo/.venv/bin/python -m coverage run --parallel-mode -m pytest -q tests"
    )
    assert plan.mode == "already-measured"
    assert (
        plan.command == "/repo/.venv/bin/python -m coverage run --parallel-mode -m pytest -q tests"
    )


def test_bare_module_invocation_without_arguments_is_wrapped() -> None:
    plan = plan_coverage_invocation("python -m pytest")
    assert plan.mode == "wrapped"
    assert plan.command == "python -m coverage run --parallel-mode -m pytest"


def test_non_module_commands_are_reported_as_unsupported() -> None:
    for command in ("make test", "bash -c 'pytest -q'", "./scripts/test.sh", "pytest -q tests"):
        plan = plan_coverage_invocation(command)
        assert plan.mode == "unsupported", command
        # The original command is preserved for the gate to report verbatim.
        assert plan.command == command
