"""Gate and stage result types.

A gate is the smallest independently reportable check. A stage is a group of
gates (for example, ``security`` contains secrets, SAST, and configuration
checks). Only gates classified ``BLOCKING`` can fail the pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

GateStatus = Literal["PASS", "FAIL", "WARN", "SKIP", "ERROR"]
Classification = Literal["BLOCKING", "WARNING", "INFO"]
StageStatus = Literal["PASS", "FAIL", "WARN", "SKIP"]


@dataclass(slots=True)
class GateResult:
    id: str
    stage: str
    title: str
    status: GateStatus
    classification: Classification
    summary: str = ""
    command: str | None = None
    exit_code: int | None = None
    duration_seconds: float = 0.0
    log: str | None = None
    remediation: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)

    @property
    def blocking_failure(self) -> bool:
        return self.classification == "BLOCKING" and self.status in {"FAIL", "ERROR"}

    @property
    def failed(self) -> bool:
        return self.status in {"FAIL", "ERROR"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "stage": self.stage,
            "title": self.title,
            "status": self.status,
            "classification": self.classification,
            "summary": self.summary,
            "command": self.command,
            "exit_code": self.exit_code,
            "duration_seconds": round(self.duration_seconds, 3),
            "log": self.log,
            "remediation": self.remediation,
            "details": self.details,
            "artifacts": self.artifacts,
        }


@dataclass(slots=True)
class StageResult:
    name: str
    status: StageStatus
    gates: list[GateResult] = field(default_factory=list)
    duration_seconds: float = 0.0
    skipped_reason: str | None = None
    error: str | None = None

    @property
    def blocking_failure(self) -> bool:
        return any(gate.blocking_failure for gate in self.gates) or self.status == "FAIL"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "duration_seconds": round(self.duration_seconds, 3),
            "skipped_reason": self.skipped_reason,
            "error": self.error,
            "gates": [gate.to_dict() for gate in self.gates],
        }


def classify(required: bool) -> Classification:
    return "BLOCKING" if required else "WARNING"


def gate(
    gate_id: str,
    stage: str,
    title: str,
    status: GateStatus,
    *,
    required: bool,
    summary: str = "",
    command: str | None = None,
    exit_code: int | None = None,
    duration_seconds: float = 0.0,
    log: str | None = None,
    remediation: str | None = None,
    details: dict[str, Any] | None = None,
    artifacts: list[str] | None = None,
    classification: Classification | None = None,
) -> GateResult:
    return GateResult(
        id=gate_id,
        stage=stage,
        title=title,
        status=status,
        classification=classification or classify(required),
        summary=summary,
        command=command,
        exit_code=exit_code,
        duration_seconds=duration_seconds,
        log=log,
        remediation=remediation,
        details=details or {},
        artifacts=artifacts or [],
    )


def passed(
    gate_id: str,
    stage: str,
    title: str,
    *,
    required: bool,
    summary: str = "",
    **kwargs: Any,
) -> GateResult:
    return gate(gate_id, stage, title, "PASS", required=required, summary=summary, **kwargs)


def failed(
    gate_id: str,
    stage: str,
    title: str,
    *,
    required: bool,
    summary: str,
    **kwargs: Any,
) -> GateResult:
    return gate(gate_id, stage, title, "FAIL", required=required, summary=summary, **kwargs)


def warning(
    gate_id: str,
    stage: str,
    title: str,
    *,
    summary: str,
    **kwargs: Any,
) -> GateResult:
    return gate(
        gate_id,
        stage,
        title,
        "WARN",
        required=False,
        summary=summary,
        classification="WARNING",
        **kwargs,
    )


def skipped(
    gate_id: str,
    stage: str,
    title: str,
    *,
    summary: str,
    **kwargs: Any,
) -> GateResult:
    return gate(gate_id, stage, title, "SKIP", required=False, summary=summary, **kwargs)


def errored(
    gate_id: str,
    stage: str,
    title: str,
    *,
    required: bool,
    summary: str,
    **kwargs: Any,
) -> GateResult:
    return gate(gate_id, stage, title, "ERROR", required=required, summary=summary, **kwargs)


def derive_stage_status(
    gates: list[GateResult], *, skipped_reason: str | None = None
) -> StageStatus:
    if any(gate.blocking_failure for gate in gates):
        return "FAIL"
    if gates and all(gate.status == "SKIP" for gate in gates):
        return "SKIP"
    if any(gate.status == "WARN" for gate in gates):
        return "WARN"
    if any(gate.status == "FAIL" for gate in gates):
        return "WARN"
    return "PASS"


def stage_result(
    name: str,
    gates: list[GateResult],
    *,
    duration_seconds: float = 0.0,
    skipped_reason: str | None = None,
    error: str | None = None,
) -> StageResult:
    status: StageStatus = (
        "FAIL" if error else derive_stage_status(gates, skipped_reason=skipped_reason)
    )
    if error and not gates:
        status = "FAIL"
    return StageResult(
        name=name,
        status=status,
        gates=gates,
        duration_seconds=duration_seconds,
        skipped_reason=skipped_reason,
        error=error,
    )
