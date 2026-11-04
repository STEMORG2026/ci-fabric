"""Format, lint and type-check stages.

These stages execute repository-configured commands. ci-fabric deliberately
does not guess formatter behavior at execution time: the generated
configuration makes the commands visible and reviewable.
"""

from __future__ import annotations

import time

from ..context import Context
from ..gates import GateResult, StageResult, skipped, stage_result
from .base import Echo, disabled_gate, disabled_skip_reason, no_command_gate, run_stage_commands


def _elapsed(started: float) -> float:
    return time.monotonic() - started


def run_check_stage(
    *,
    context: Context,
    name: str,
    echo: Echo,
    fix_mode: bool = False,
) -> StageResult:
    started = time.monotonic()
    stage = context.config.stage(name)
    if stage is None:
        gates: list[GateResult] = [
            skipped(
                f"{name}.config",
                name,
                f"{name} configured",
                summary=f"[stages.{name}] is not configured",
            )
        ]
        return stage_result(
            name,
            gates,
            duration_seconds=_elapsed(started),
            skipped_reason="not configured",
        )
    if not stage.enabled:
        return stage_result(
            name,
            [disabled_gate(stage=stage)],
            duration_seconds=_elapsed(started),
            skipped_reason=disabled_skip_reason(stage),
        )
    commands = stage.fix_commands if fix_mode else stage.commands
    if not commands:
        if fix_mode and stage.commands:
            return stage_result(
                name,
                [
                    skipped(
                        f"{name}.fix",
                        name,
                        f"{name} auto-fix",
                        summary="no fix command configured; nothing was modified",
                    )
                ],
                duration_seconds=_elapsed(started),
            )
        gates = [
            no_command_gate(
                stage=stage,
                context=context,
                title=f"{name} command",
                purpose=f"{name} gate",
            )
        ]
        return stage_result(
            name,
            gates,
            duration_seconds=_elapsed(started),
            skipped_reason=None if stage.required else "not configured",
        )
    gates, _ = run_stage_commands(
        context=context,
        stage=stage,
        echo=echo,
        commands=commands,
    )
    return stage_result(name, gates, duration_seconds=_elapsed(started))
