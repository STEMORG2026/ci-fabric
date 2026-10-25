"""Shared helpers for stage implementations."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from ..config import StageConfig
from ..context import Context
from ..gates import GateResult, failed, passed, skipped
from ..runner import CommandResult, exit_remediation, run_command
from ..util import relpath

Echo = Callable[[str], None]


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
            gates.append(
                failed(
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
