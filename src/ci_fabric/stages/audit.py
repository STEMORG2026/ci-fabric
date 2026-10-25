"""Dependency vulnerability audit stage."""

from __future__ import annotations

import time

from ..config import StageConfig
from ..context import Context
from ..gates import GateResult, StageResult, failed, skipped, stage_result
from .base import Echo, disabled_gate, run_stage_commands


def run_audit_stage(context: Context, echo: Echo) -> StageResult:
    started = time.monotonic()
    configured = context.config.stage("audit")
    required = bool(
        (configured and configured.required)
        or context.config.policy.audit_required
        or "audit" in context.config.effective_required_stages()
    )
    stage = configured or StageConfig(
        name="audit",
        required=required,
        timeout_seconds=900,
    )
    if configured and not configured.enabled:
        return stage_result(
            "audit",
            [disabled_gate(stage=configured)],
            duration_seconds=time.monotonic() - started,
            skipped_reason="disabled",
        )
    gates: list[GateResult] = []
    if not stage.commands:
        if required:
            gates.append(
                failed(
                    "audit.config",
                    "audit",
                    "dependency audit",
                    required=True,
                    summary="dependency audit is required but no command is configured",
                    remediation=(
                        "Configure [stages.audit] command. Examples: "
                        "`pip-audit` for Python, `npm audit --audit-level=high` for npm, "
                        "`pnpm audit --audit-level=high`, `cargo audit`, or `govulncheck ./...`."
                    ),
                )
            )
        else:
            gates.append(
                skipped(
                    "audit.config",
                    "audit",
                    "dependency audit",
                    summary="no dependency audit configured (not required by policy)",
                )
            )
        return stage_result(
            "audit",
            gates,
            duration_seconds=time.monotonic() - started,
            skipped_reason=None if required else "not configured",
        )

    command_gates, _ = run_stage_commands(
        context=context,
        stage=stage,
        echo=echo,
        gate_stage="audit",
    )
    for index, gate in enumerate(command_gates, start=1):
        gate.id = f"audit.dependencies.{index:02d}"
        gate.title = f"dependency audit: {gate.command or gate.title}"
        if not gate.failed:
            gate.summary = "no known vulnerabilities at configured severity"
        gates.append(gate)
    return stage_result("audit", gates, duration_seconds=time.monotonic() - started)
