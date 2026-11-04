"""Shared helpers for stage implementations."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from ..config import StageConfig
from ..context import Context
from ..gates import GateResult, errored, failed, passed, skipped
from ..runner import CommandResult, exit_remediation, run_command
from ..util import relpath

Echo = Callable[[str], None]

# Test tiers that measure their own coverage write parallel data files under
# this basename inside the run's artifact directory; the coverage stage combines
# whatever it finds there. One name, two stages, no hand-wired paths: the tiers
# export COVERAGE_FILE={artifacts}/coverage.data and the coverage stage globs
# coverage.data.* in the same directory.
COVERAGE_DATA_NAME = "coverage.data"


def coverage_data_files(context: Context) -> list[Path]:
    """Parallel data files the test tiers measured into this run."""
    return sorted(context.paths.artifacts_dir.glob(f"{COVERAGE_DATA_NAME}.*"))


def log_path(context: Context, stage: str, index: int) -> Path:
    return context.paths.logs_dir / f"{stage}-{index:02d}.log"


def gate_prefix(stage: str, index: int) -> str:
    return f"{stage}.{index:02d}"


def run_stage_commands(
    *,
    context: Context,
    stage: StageConfig,
    echo: Echo,
    commands: Sequence[str] | None = None,
    gate_stage: str | None = None,
    stop_on_failure: bool = True,
    extra_env: dict[str, str] | None = None,
) -> tuple[list[GateResult], list[CommandResult]]:
    gates: list[GateResult] = []
    results: list[CommandResult] = []
    selected = list(commands if commands is not None else stage.commands)
    if not selected:
        return gates, results
    cwd = context.stage_cwd(stage)
    environment = context.stage_env(stage, extra=extra_env)
    stage_label = gate_stage or stage.name
    for index, raw_command in enumerate(selected, start=1):
        if any(gate.blocking_failure for gate in gates) and stop_on_failure:
            break
        command = context.render(raw_command)
        path = log_path(context, stage_label, index)
        echo(f"  $ {command}")
        result = run_command(
            command,
            cwd=cwd,
            env=environment,
            timeout=float(stage.timeout_seconds),
            log_path=path,
            echo=echo,
        )
        results.append(result)
        relative_log = relpath(path, context.root)
        remediation = exit_remediation(result)
        summary = result.summary()
        if len(result.output_tail.strip().splitlines()) > 0 and not result.ok:
            last_lines = result.output_tail.strip().splitlines()[-5:]
            summary = f"{summary}: {' | '.join(line.strip() for line in last_lines)}"[:500]
        if result.ok:
            gates.append(
                passed(
                    f"{stage_label}.{index:02d}",
                    stage_label,
                    command,
                    required=stage.required,
                    summary=summary,
                    command=command,
                    exit_code=result.exit_code,
                    duration_seconds=result.duration_seconds,
                    log=relative_log,
                )
            )
        else:
            # Three outcomes, not two (pattern adopted from USA): a missing
            # tool, a process that never started, or a timeout is the *absence*
            # of a verdict, not a verdict of fail. "trivy is not installed"
            # must never be recorded as "trivy found something". Enforcement is
            # unchanged — ERROR on a blocking gate blocks exactly like FAIL —
            # only the machine-readable label changes.
            no_verdict = result.timed_out or result.exit_code is None or result.exit_code == 127
            factory = errored if no_verdict else failed
            extra_details: dict[str, Any] = {"outcome": "unverified"} if no_verdict else {}
            gates.append(
                factory(
                    f"{stage_label}.{index:02d}",
                    stage_label,
                    command,
                    required=stage.required,
                    summary=summary,
                    command=command,
                    exit_code=result.exit_code,
                    duration_seconds=result.duration_seconds,
                    log=relative_log,
                    remediation=remediation,
                    details=extra_details,
                )
            )
    return gates, results


def no_command_gate(
    *,
    stage: StageConfig,
    context: Context,
    title: str,
    purpose: str,
) -> GateResult:
    if stage.required:
        return failed(
            f"{stage.name}.config",
            stage.name,
            title,
            required=True,
            summary=f"{purpose} is required but no command is configured",
            remediation=(
                f"Configure [stages.{stage.name}] command, or set required=false "
                f"if this gate deliberately does not apply."
            ),
        )
    return skipped(
        f"{stage.name}.config",
        stage.name,
        title,
        summary=f"{purpose} is not configured",
    )


def disabled_gate(*, stage: StageConfig) -> GateResult:
    return skipped(
        f"{stage.name}.disabled",
        stage.name,
        f"{stage.name} disabled",
        summary=f"[stages.{stage.name}] enabled=false",
    )
