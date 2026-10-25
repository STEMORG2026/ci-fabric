"""Pipeline orchestration: canonical order, fail-fast, and release hand-off."""

from __future__ import annotations

import time
import traceback
from collections.abc import Sequence
from dataclasses import dataclass

from .config import PIPELINE_STAGES, Config
from .context import Context
from .errors import CiError
from .gates import StageResult, errored, stage_result
from .release import create_release
from .report import RunResult
from .stages.artifact import run_artifact_stage
from .stages.audit import run_audit_stage
from .stages.base import Echo
from .stages.build import run_build_stage
from .stages.coverage import run_coverage_stage
from .stages.package import run_package_stage
from .stages.precheck import run_precheck_stage
from .stages.quality import run_check_stage
from .stages.repository import run_repository_stage
from .stages.security import run_security_stage
from .stages.tests import run_test_stage
from .util import human_duration, isoformat, utc_now

CANONICAL_ORDER: tuple[str, ...] = (
    "precheck",
    "repository",
    "format",
    "lint",
    "typecheck",
    "unit",
    "integration",
    "contract",
    "e2e",
    "coverage",
    "security",
    "audit",
    "build",
    "package",
    "artifact",
)

COMMAND_STAGES: dict[str, tuple[str, ...]] = {
    "ci": tuple(name for name in CANONICAL_ORDER if name != "precheck"),
    "release": tuple(name for name in CANONICAL_ORDER if name != "precheck"),
    "validate": ("repository",),
    "check": ("format", "lint", "typecheck", "security"),
    "format": ("format",),
    "lint": ("lint",),
    "typecheck": ("typecheck",),
    "test": ("unit", "integration", "contract", "e2e", "coverage"),
    "unit": ("unit",),
    "integration": ("integration",),
    "contract": ("contract",),
    "e2e": ("e2e",),
    "coverage": ("coverage",),
    "security": ("security", "audit"),
    "audit": ("audit",),
    "build": ("build", "package", "artifact"),
    "package": ("package", "artifact"),
    "artifact": ("artifact",),
}


@dataclass(slots=True)
class ReleaseRequest:
    version: str | None = None
    tag: bool = False
    notes_file: str | None = None
    force: bool = False


def stages_for_command(command: str) -> list[str]:
    try:
        return list(COMMAND_STAGES[command])
    except KeyError as exc:
        raise CiError(
            f"no pipeline mapping for command: {command}",
            remediation=f"Known commands: {', '.join(sorted(COMMAND_STAGES))}.",
        ) from exc


def apply_effective_requirements(config: Config) -> list[str]:
    """Promote profile-required stages to BLOCKING in the effective config.

    Configuration remains the source of truth; the profile is also a declared
    policy input. Promotion is recorded in the result metadata.
    """
    promoted: list[str] = []
    required = set(config.effective_required_stages())
    for name in required:
        stage = config.stages.get(name)
        if stage is not None and not stage.required:
            stage.required = True
            promoted.append(name)
    return promoted


def _skip(name: str, reason: str) -> StageResult:
    return StageResult(name=name, status="SKIP", skipped_reason=reason)


def _dispatch(
    *,
    context: Context,
    name: str,
    echo: Echo,
    fix: bool,
) -> StageResult:
    if name == "precheck":
        return run_precheck_stage(context, echo)
    if name == "repository":
        return run_repository_stage(context, echo)
    if name in {"format", "lint", "typecheck"}:
        return run_check_stage(
            context=context,
            name=name,
            echo=echo,
            fix_mode=fix and name == "format",
        )
    if name in {"unit", "integration", "contract", "e2e"}:
        return run_test_stage(context=context, name=name, echo=echo)
    if name == "coverage":
        return run_coverage_stage(context, echo)
    if name == "security":
        return run_security_stage(context, echo)
    if name == "audit":
        return run_audit_stage(context, echo)
    if name == "build":
        return run_build_stage(context, echo)
    if name == "package":
        return run_package_stage(context, echo)
    if name == "artifact":
        return run_artifact_stage(context, echo)
    raise CiError(f"unknown pipeline stage: {name}")


def _required_for_stage(context: Context, name: str) -> bool:
    stage = context.config.stages.get(name)
    if stage is not None:
        return bool(stage.required)
    return name in context.config.effective_required_stages() or name in {
        "precheck",
        "repository",
    }


def _failed_stage(context: Context, name: str, error: str, elapsed: float) -> StageResult:
    required = _required_for_stage(context, name)
    gate = errored(
        f"{name}.internal",
        name,
        f"{name} stage failed to run",
        required=required,
        summary=error,
        remediation=(
            "Fix the reported configuration or environment problem. If the message "
            "indicates an internal defect, capture the run log and report it."
        ),
    )
    return stage_result(
        name,
        [gate],
        duration_seconds=elapsed,
        error=error,
    )


def _print_stage(stage: StageResult, echo: Echo) -> None:
    for gate in stage.gates:
        if gate.status == "PASS":
            continue
        marker = {
            "FAIL": "FAIL",
            "ERROR": "ERROR",
            "WARN": "WARN",
            "SKIP": "skip",
        }.get(gate.status, gate.status)
        summary = gate.summary if len(gate.summary) <= 220 else gate.summary[:217] + "..."
        echo(f"    {marker:5} {gate.id}: {summary}")
        if gate.blocking_failure and gate.log:
            echo(f"          log: {gate.log}")
        if gate.blocking_failure and gate.remediation:
            echo(f"          fix: {gate.remediation}")


def run_pipeline(
    context: Context,
    *,
    command: str,
    stages: Sequence[str] | None = None,
    fix: bool = False,
    release: ReleaseRequest | None = None,
    echo: Echo = print,
) -> RunResult:
    config = context.config
    requested = list(stages) if stages is not None else stages_for_command(command)
    unknown = sorted(set(requested) - set(PIPELINE_STAGES))
    if unknown:
        raise CiError(
            f"unknown stage(s): {', '.join(unknown)}",
            remediation=f"Known stages: {', '.join(PIPELINE_STAGES)}.",
        )
    selected = [name for name in CANONICAL_ORDER if name in requested]
    if "precheck" not in selected:
        selected.insert(0, "precheck")
    promoted = apply_effective_requirements(config)
    started = utc_now()
    results: list[StageResult] = []
    failure_seen = False
    release_payload: dict[str, object] | None = None
    release_error: str | None = None
    metadata: dict[str, object] = {
        "selected_stages": selected,
        "promoted_to_blocking": promoted,
        "fail_fast": config.policy.fail_fast,
        "clean_env": context.clean_env,
    }

    if release is not None and config.release.require_clean and context.git.dirty:
        message = "working tree is dirty; release requires a committed, reproducible revision"
        gate = errored(
            "release.clean-tree",
            "precheck",
            "release precondition",
            required=True,
            summary=message,
            remediation=(
                "Commit or stash the changes, then re-run `./ci release`. "
                "Do not release an uncommitted tree."
            ),
        )
        results.append(stage_result("precheck", [gate], duration_seconds=0.0, error=message))
        finished = utc_now()
        return RunResult(
            context=context,
            command=command,
            stages=results,
            status="FAIL",
            exit_code=1,
            started_at=isoformat(started),
            finished_at=isoformat(finished),
            duration_seconds=max(0.0, (finished - started).total_seconds()),
            release={"ok": False, "error": message},
            error=message,
            metadata=metadata,
        )

    total = len(selected)
    for index, name in enumerate(selected, start=1):
        if failure_seen and config.policy.fail_fast:
            results.append(_skip(name, "fail-fast after a blocking failure"))
            echo(f"[{index}/{total}] {name}: skipped (fail-fast)")
            continue
        echo("")
        echo(f"[{index}/{total}] {name}")
        stage_started = time.monotonic()
        try:
            stage = _dispatch(context=context, name=name, echo=echo, fix=fix)
        except CiError as exc:
            stage = _failed_stage(context, name, str(exc), time.monotonic() - stage_started)
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            traceback.print_exc()
            stage = _failed_stage(context, name, detail, time.monotonic() - stage_started)
        if stage.duration_seconds <= 0:
            stage.duration_seconds = time.monotonic() - stage_started
        results.append(stage)
        _print_stage(stage, echo)
        if stage.blocking_failure:
            failure_seen = True
        echo(
            f"  {stage.status} {name} ({human_duration(stage.duration_seconds)})"
            + (f" — {stage.skipped_reason}" if stage.skipped_reason else "")
        )

    if release is not None and not failure_seen:
        echo("")
        echo("[release] creating verified local release bundle")
        try:
            record = create_release(
                context,
                version=release.version,
                tag=release.tag,
                notes_file=release.notes_file,
                force=release.force,
                echo=echo,
            )
            release_payload = {"ok": True, **record.to_dict(context.root)}
        except CiError as exc:
            release_error = str(exc)
            release_payload = {
                "ok": False,
                "error": str(exc),
                "remediation": exc.remediation,
            }
            failure_seen = True
    elif release is not None:
        release_error = "release skipped because the pipeline did not pass"
        release_payload = {"ok": False, "error": release_error}

    finished = utc_now()
    blocking = any(stage.blocking_failure for stage in results)
    # A stage is WARN when a gate warned or an advisory (non-blocking) gate
    # failed; surface that honestly at run level while still exiting 0.
    warnings = any(stage.status == "WARN" for stage in results)
    status = "FAIL" if blocking or release_error else ("WARN" if warnings else "PASS")
    exit_code = 1 if status == "FAIL" else 0
    return RunResult(
        context=context,
        command=command,
        stages=results,
        status=status,
        exit_code=exit_code,
        started_at=isoformat(started),
        finished_at=isoformat(finished),
        duration_seconds=max(0.0, (finished - started).total_seconds()),
        release=release_payload,
        error=release_error,
        metadata=metadata,
    )


def describe_pipeline(config: Config) -> list[dict[str, object]]:
    """Return the canonical stage order with effective classification."""
    descriptions: list[dict[str, object]] = []
    required = set(config.effective_required_stages())
    for name in CANONICAL_ORDER:
        stage = config.stages.get(name)
        configured = stage is not None
        if name in {"precheck", "repository"}:
            enabled = True
        elif stage is None:
            enabled = name == "security"
        else:
            enabled = stage.enabled
        required_effective = name in required or bool(stage and stage.required)
        if name in {"precheck", "repository"}:
            required_effective = True
        descriptions.append(
            {
                "stage": name,
                "configured": configured,
                "enabled": enabled,
                "required": required_effective,
                "classification": "BLOCKING" if required_effective else "WARNING",
            }
        )
    return descriptions
