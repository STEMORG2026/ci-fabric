"""PRECHECK stage: environment facts and non-negotiable preconditions."""

from __future__ import annotations

import shutil
import sys

from ..config import StageConfig
from ..context import Context
from ..gates import GateResult, StageResult, failed, passed, stage_result, warning
from ..util import ensure_dir, git_is_ignored, relpath, utc_now, write_json
from .base import Echo


def run_precheck_stage(context: Context, echo: Echo) -> StageResult:
    gates: list[GateResult] = []
    stage = StageConfig(name="precheck", required=True, timeout_seconds=60)

    gates.append(
        passed(
            "precheck.config",
            "precheck",
            "configuration is valid",
            required=True,
            summary=f"{relpath(context.config.source_path, context.root)} (schema 1)",
        )
    )

    git = context.git
    if git.available:
        gates.append(
            passed(
                "precheck.git",
                "precheck",
                "Git repository",
                required=True,
                summary=(
                    f"{git.branch or 'detached'} @ {git.short_revision or 'no commits'}; "
                    f"{'dirty' if git.dirty else 'clean'}; {git.tracked_files} tracked files"
                ),
                details=git.to_dict(),
            )
        )
    else:
        gates.append(
            passed(
                "precheck.git",
                "precheck",
                "Git repository",
                required=stage.required,
                summary="not a Git repository (policy.allow_no_git=true)",
            )
        )

    ci_ignored = False
    if git.available:
        ci_ignored = git_is_ignored(context.root, ".ci/")
    if git.available and not ci_ignored:
        gates.append(
            warning(
                "precheck.state-ignored",
                "precheck",
                "CI output is ignored by Git",
                summary=".ci/ is not ignored; run output can be accidentally committed",
                remediation="Add `.ci/` and `.localci/` to .gitignore (or run `./ci init`).",
            )
        )
    else:
        gates.append(
            passed(
                "precheck.state-ignored",
                "precheck",
                "CI output is ignored by Git",
                required=False,
                summary=".ci/ is ignored or Git is unavailable",
            )
        )

    try:
        usage = shutil.disk_usage(context.root)
        free_gib = usage.free / (1024**3)
        if free_gib < 1.0:
            gates.append(
                warning(
                    "precheck.disk",
                    "precheck",
                    "free disk space",
                    summary=f"only {free_gib:.1f} GiB free",
                    remediation="Free disk space before running build/package stages.",
                )
            )
        else:
            gates.append(
                passed(
                    "precheck.disk",
                    "precheck",
                    "free disk space",
                    required=False,
                    summary=f"{free_gib:.1f} GiB free",
                )
            )
    except OSError as exc:
        gates.append(
            warning(
                "precheck.disk",
                "precheck",
                "free disk space",
                summary=f"could not inspect disk usage: {exc}",
            )
        )

    if context.config.policy.require_clean_worktree == "always":
        if git.dirty:
            gates.append(
                failed(
                    "precheck.clean",
                    "precheck",
                    "clean working tree",
                    required=True,
                    summary=f"{len(git.dirty_files)} modified/untracked path(s)",
                    details={"paths": git.dirty_files[:100]},
                    remediation="Commit or stash changes; remove stale generated files.",
                )
            )
        else:
            gates.append(
                passed(
                    "precheck.clean",
                    "precheck",
                    "clean working tree",
                    required=True,
                    summary="clean",
                )
            )

    python_supported = sys.version_info >= (3, 11)
    if python_supported:
        gates.append(
            passed(
                "precheck.python",
                "precheck",
                "Python runtime",
                required=True,
                summary=(
                    f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
                ),
            )
        )
    else:
        gates.append(
            failed(
                "precheck.python",
                "precheck",
                "Python runtime",
                required=True,
                summary=(
                    f"Python {sys.version_info.major}.{sys.version_info.minor} is too old; "
                    "3.11+ required"
                ),
                remediation="Run ci-fabric with Python 3.11 or newer.",
            )
        )

    shell = shutil.which("sh") or shutil.which("bash")
    gates.append(
        passed(
            "precheck.shell",
            "precheck",
            "POSIX shell",
            required=True,
            summary=shell or "missing",
            remediation=None if shell else "Install /bin/sh (required for configured commands).",
        )
        if shell
        else failed(
            "precheck.shell",
            "precheck",
            "POSIX shell",
            required=True,
            summary="no POSIX shell found",
            remediation="Install /bin/sh; command execution is POSIX-only.",
        )
    )

    environment_path = context.paths.reports_dir / "environment.json"
    ensure_dir(environment_path.parent)
    write_json(
        environment_path,
        {
            **context.environment_snapshot(),
            "finished_at": utc_now().isoformat().replace("+00:00", "Z"),
        },
    )
    gates.append(
        passed(
            "precheck.environment",
            "precheck",
            "environment recorded",
            required=False,
            summary=relpath(environment_path, context.root),
            artifacts=[relpath(environment_path, context.root)],
        )
    )

    echo(
        f"  {context.config.project.name} {context.version} "
        f"@ {context.git.short_revision or 'nogit'} "
        f"[{context.config.policy.profile}] run {context.run_id}"
    )
    return stage_result("precheck", gates)
