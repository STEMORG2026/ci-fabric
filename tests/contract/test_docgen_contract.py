"""Contracts for the generated reference and the command tables behind it."""

from __future__ import annotations

from pathlib import Path

import pytest

from ci_fabric import __version__
from ci_fabric.commands import (
    AUX_COMMAND_HELP,
    DEPLOY_COMMAND_HELP,
    PIPELINE_COMMAND_HELP,
    PIPELINE_COMMANDS,
)
from ci_fabric.config import load_config
from ci_fabric.context import create_context
from ci_fabric.docgen import (
    GENERATED_MARKER,
    REFERENCE_RELATIVE_PATH,
    generate_reference,
)
from ci_fabric.errors import (
    EXIT_CONFIG,
    EXIT_GATE_FAILED,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_PREREQUISITE,
)
from ci_fabric.pipeline import CANONICAL_ORDER, COMMAND_STAGES
from tests.support import make_repo


@pytest.fixture()
def reference(tmp_path: Path) -> str:
    root = make_repo(tmp_path)
    config = load_config(root)
    context = create_context(root, config)
    return generate_reference(config, context)


def test_reference_is_deterministic_and_stamped(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    config = load_config(root)
    context = create_context(root, config)
    assert generate_reference(config, context) == generate_reference(config, context)
    assert f"ci-fabric {__version__}" in generate_reference(config, context)
    assert GENERATED_MARKER in generate_reference(config, context)


def test_reference_contains_the_full_contract(reference: str) -> None:
    # Every canonical stage, in order.
    for stage in CANONICAL_ORDER:
        assert f"`{stage}`" in reference
    order_text = " → ".join(CANONICAL_ORDER)
    assert order_text.split(" → ")[0] in reference
    # Every exit code.
    for code in (EXIT_OK, EXIT_GATE_FAILED, EXIT_CONFIG, EXIT_PREREQUISITE, EXIT_INTERRUPTED):
        assert f"| `{code}` |" in reference
    # Every command from every table, with its purpose.
    for name, help_text in PIPELINE_COMMAND_HELP + DEPLOY_COMMAND_HELP + AUX_COMMAND_HELP:
        assert f"`./ci {name}`" in reference, name
        assert help_text in reference, name
    # Placeholders are listed.
    assert "`{python}`" in reference
    assert "`{artifacts}`" in reference


def test_reference_documents_stage_mappings(reference: str) -> None:
    for name, _ in PIPELINE_COMMAND_HELP:
        stages = COMMAND_STAGES.get(name)
        if stages:
            assert ", ".join(stages) in reference, name


def test_command_tables_cover_the_parser_exactly() -> None:
    """The tables are the single source of truth: every table entry must be a
    real subcommand with the same help text, and every real subcommand must
    appear in some table."""
    from ci_fabric.cli import _build_parser

    parser = _build_parser()
    subparsers = parser._subparsers._group_actions[0]
    registered = {action.dest: action.help for action in subparsers._choices_actions}

    table = dict(PIPELINE_COMMAND_HELP + DEPLOY_COMMAND_HELP + AUX_COMMAND_HELP)
    table["init"] = "generate ci.toml and the ./ci entry point"
    table["release"] = "run the full pipeline and create a local release"
    # hook-run is the hidden hook-dispatcher entry point.
    registered.pop("hook-run")
    assert set(table) == set(registered), (
        f"missing from tables: {sorted(set(registered) - set(table))}; "
        f"missing from parser: {sorted(set(table) - set(registered))}"
    )
    for name, help_text in table.items():
        assert registered[name] == help_text, name


def test_pipeline_commands_tuple_matches_help_table() -> None:
    help_names = {name for name, _ in PIPELINE_COMMAND_HELP}
    # run and release are pipeline commands defined outside the help loop.
    assert set(PIPELINE_COMMANDS) - help_names == {"run", "release"}


def test_reference_path_is_stable() -> None:
    assert REFERENCE_RELATIVE_PATH == "docs/reference.md"
