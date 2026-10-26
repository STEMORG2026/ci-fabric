"""Command surface tables — the single source of truth.

Both the CLI parser and the generated documentation reference consume these
tables, so a command cannot exist without appearing in `docs/reference.md`,
and the reference cannot mention a command the parser does not have.
"""

from __future__ import annotations

PIPELINE_COMMANDS: tuple[str, ...] = (
    "validate",
    "check",
    "test",
    "build",
    "package",
    "security",
    "audit",
    "format",
    "lint",
    "typecheck",
    "unit",
    "integration",
    "contract",
    "e2e",
    "coverage",
    "artifact",
    "ci",
    "run",
    "release",
)

PIPELINE_COMMAND_HELP: tuple[tuple[str, str], ...] = (
    ("validate", "repository integrity gates"),
    ("check", "format, lint, static analysis and secret scanning"),
    ("test", "unit, integration, contract, e2e and coverage"),
    ("build", "build, package and validate artifacts"),
    ("package", "package and validate the release artifact"),
    ("security", "secret scan, SAST and dependency audit"),
    ("audit", "dependency audit only"),
    ("format", "format check (or --fix)"),
    ("lint", "lint"),
    ("typecheck", "static type checking"),
    ("unit", "unit tests"),
    ("integration", "integration tests"),
    ("contract", "contract tests"),
    ("e2e", "end-to-end tests"),
    ("coverage", "coverage report and threshold"),
    ("artifact", "artifact validation only"),
    ("ci", "full local pipeline"),
)

DEPLOY_COMMAND_HELP: tuple[tuple[str, str], ...] = (
    ("deploy", "deploy a verified local release"),
    ("deploy-status", "show deployment state"),
    ("rollback", "roll back to the previous deployment"),
    ("undeploy", "stop the current deployment"),
    ("health", "check the current deployment health"),
    ("smoke", "run the current deployment smoke checks"),
)

AUX_COMMAND_HELP: tuple[tuple[str, str], ...] = (
    ("doctor", "diagnose environment and tooling problems"),
    ("policy", "show the effective policy"),
    ("status", "show repository, run, hook and deployment status"),
    ("releases", "list verified local releases"),
    ("hooks", "install, remove or inspect Git hooks"),
    ("clean", "remove old run state"),
    ("run", "run an explicit stage selection"),
    ("docs", "regenerate docs/reference.md from the live code"),
    ("version", "print the tool version"),
)

# Commands whose stage mapping is not a fixed tuple (argued in the reference).
RUN_STAGES = "explicit `--stages`/`--skip` selection"
RELEASE_STAGES = "the full pipeline, then a verified local release bundle"
