"""Structured result documents, human summaries, and run retention."""

from __future__ import annotations

import contextlib
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__
from .context import Context
from .gates import GateResult, StageResult
from .util import (
    ensure_dir,
    human_duration,
    isoformat,
    relpath,
    remove_path,
    sha256_file,
    utc_now,
    write_json,
    write_text_atomic,
)

RESULT_SCHEMA = 1


@dataclass(slots=True)
class RunResult:
    context: Context
    command: str
    stages: list[StageResult]
    status: str
    exit_code: int
    started_at: str
    finished_at: str
    duration_seconds: float
    release: dict[str, Any] | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def all_gates(self) -> list[GateResult]:
        return [gate for stage in self.stages for gate in stage.gates]

    @property
    def blocking_failures(self) -> list[GateResult]:
        return [gate for gate in self.all_gates if gate.blocking_failure]

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {
            "stages": len(self.stages),
            "gates": 0,
            "blocking_failures": 0,
            "warning_gates": 0,
        }
        for status in ("pass", "fail", "warn", "skip", "error"):
            counts[status] = 0
        for gate in self.all_gates:
            counts["gates"] += 1
            key = gate.status.lower()
            if key in counts:
                counts[key] += 1
            if gate.blocking_failure:
                counts["blocking_failures"] += 1
            if gate.classification == "WARNING" and gate.status in {"WARN", "FAIL", "ERROR"}:
                counts["warning_gates"] += 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        context = self.context
        artifacts: list[str] = []
        for gate in self.all_gates:
            for artifact in gate.artifacts:
                if artifact and artifact not in artifacts:
                    artifacts.append(artifact)
        payload: dict[str, Any] = {
            "schema_version": RESULT_SCHEMA,
            "tool": {"name": "ci-fabric", "version": __version__},
            "command": self.command,
            "status": self.status,
            "exit_code": self.exit_code,
            "project": {
                "name": context.config.project.name,
                "version": context.config.project.version,
                "profile": context.config.policy.profile,
            },
            "run": {
                "id": context.run_id,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "duration_seconds": round(self.duration_seconds, 3),
                "git": context.git.to_dict(),
                "paths": context.paths.to_dict(context.root),
            },
            "environment": context.environment_snapshot(),
            "policy": context.config.policy_document(),
            "counts": self.counts(),
            "stages": [stage.to_dict() for stage in self.stages],
            "blocking_failures": [
                {
                    "id": gate.id,
                    "stage": gate.stage,
                    "summary": gate.summary,
                    "log": gate.log,
                    "remediation": gate.remediation,
                }
                for gate in self.blocking_failures
            ],
            "artifacts": artifacts,
            "release": self.release,
            "error": self.error,
            "metadata": self.metadata,
        }
        return payload


def write_policy(context: Context) -> Path:
    document = {
        "schema_version": 1,
        "tool": {"name": "ci-fabric", "version": __version__},
        "config": relpath(context.config.source_path, context.root),
        "config_sha256": sha256_file(context.config.source_path),
        "policy": context.config.policy_document(),
    }
    path = context.paths.state_dir / "policy.json"
    write_json(path, document)
    run_copy = context.paths.reports_dir / "policy.json"
    write_json(run_copy, document)
    return path


def _markdown_summary(document: dict[str, Any]) -> str:
    project = document["project"]
    run = document["run"]
    counts = document["counts"]
    lines = [
        f"# CI run {run['id']}",
        "",
        f"- Project: **{project['name']}** {project['version'] or 'unversioned'}",
        f"- Profile: `{project['profile']}`",
        f"- Command: `{document['command']}`",
        f"- Result: **{document['status']}** (exit {document['exit_code']})",
        f"- Duration: {human_duration(document['run']['duration_seconds'])}",
        f"- Commit: `{run['git']['short_revision'] or 'nogit'}` on "
        f"`{run['git']['branch'] or 'no branch'}` "
        f"({'dirty' if run['git']['dirty'] else 'clean'})",
        f"- Gates: {counts['pass']} pass, {counts['fail']} fail, {counts['warn']} warn, "
        f"{counts['skip']} skip, {counts['error']} error",
        "",
        "| Stage | Status | Duration | Gates |",
        "| --- | --- | --- | --- |",
    ]
    lines.extend(
        f"| {stage['name']} | {stage['status']} | "
        f"{human_duration(stage['duration_seconds'])} | {len(stage['gates'])} |"
        for stage in document["stages"]
    )
    if document["blocking_failures"]:
        lines.extend(["", "## Blocking failures", ""])
        for failure in document["blocking_failures"]:
            lines.append(f"### `{failure['id']}` ({failure['stage']})")
            lines.append("")
            lines.append(failure["summary"])
            if failure.get("log"):
                lines.append(f"- Log: `{failure['log']}`")
            if failure.get("remediation"):
                lines.append(f"- Remediation: {failure['remediation']}")
            lines.append("")
    warnings = [
        gate for stage in document["stages"] for gate in stage["gates"] if gate["status"] == "WARN"
    ]
    if warnings:
        lines.extend(["", "## Warnings", ""])
        lines.extend(f"- `{gate['id']}`: {gate['summary']}" for gate in warnings[:100])
    if document.get("release"):
        release = document["release"]
        lines.extend(["", "## Release", ""])
        if release.get("ok"):
            lines.append(
                f"- Version `{release.get('version')}` written to `{release.get('directory')}`"
            )
            if release.get("tag"):
                lines.append(f"- Tag: `{release['tag']}`")
        else:
            lines.append(f"- Release failed: {release.get('error')}")
    lines.extend(
        [
            "",
            "## Evidence",
            "",
            f"- Result JSON: `{run['paths']['result_path']}`",
            f"- Logs: `{run['paths']['logs_dir']}`",
            f"- Reports: `{run['paths']['reports_dir']}`",
            f"- Artifacts: `{run['paths']['artifacts_dir']}`",
            "",
        ]
    )
    return "\n".join(lines)


def write_result(result: RunResult, *, echo: Any = print) -> dict[str, Any]:
    document = result.to_dict()
    result_path = result.context.paths.result_path
    write_json(result_path, document)
    write_json(result.context.paths.latest_path, document)
    summary_path = result.context.paths.reports_dir / "summary.md"
    write_text_atomic(summary_path, _markdown_summary(document))
    write_policy(result.context)
    counts = document["counts"]
    echo("")
    echo(
        f"RESULT {document['status']} in "
        f"{human_duration(document['run']['duration_seconds'])}: "
        f"{counts['pass']} pass, {counts['fail']} fail, {counts['warn']} warn, "
        f"{counts['skip']} skip, {counts['error']} error"
    )
    echo(f"  result: {relpath(result_path, result.context.root)}")
    echo(f"  summary: {relpath(summary_path, result.context.root)}")
    if document["blocking_failures"]:
        echo("  blocking failures:")
        for failure in document["blocking_failures"][:10]:
            echo(f"    - {failure['id']}: {failure['summary']}")
            if failure.get("log"):
                echo(f"      log: {failure['log']}")
            if failure.get("remediation"):
                echo(f"      fix: {failure['remediation']}")
    return document


def prune_runs(context: Context, keep: int) -> list[str]:
    """Delete old run directories, keeping the newest *keep* result documents."""
    keep = max(1, keep)
    results_dir = context.paths.results_dir
    if not results_dir.is_dir():
        return []
    candidates = sorted(
        (path for path in results_dir.glob("*.json") if path.name != "latest.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    removed: list[str] = []
    for path in candidates[keep:]:
        run_id = path.stem
        if run_id == context.run_id:
            continue
        removed.append(run_id)
        with contextlib.suppress(OSError):
            path.unlink()
        for collection in ("logs", "reports", "artifacts", "tmp"):
            directory = context.paths.state_dir / collection / run_id
            if directory.is_dir():
                shutil.rmtree(directory, ignore_errors=True)
    return removed


def latest_result(context: Context) -> dict[str, Any] | None:
    path = context.paths.latest_path
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def report_paths(context: Context) -> dict[str, str]:
    return {
        "result": relpath(context.paths.result_path, context.root),
        "latest": relpath(context.paths.latest_path, context.root),
        "summary": relpath(context.paths.reports_dir / "summary.md", context.root),
        "logs": relpath(context.paths.logs_dir, context.root),
        "reports": relpath(context.paths.reports_dir, context.root),
        "artifacts": relpath(context.paths.artifacts_dir, context.root),
    }


def remove_run(context: Context, run_id: str) -> bool:
    removed = False
    for collection in ("logs", "reports", "artifacts", "tmp"):
        directory = context.paths.state_dir / collection / run_id
        if directory.is_dir():
            remove_path(directory)
            removed = True
    result = context.paths.results_dir / f"{run_id}.json"
    if result.is_file():
        result.unlink()
        removed = True
    return removed


def finalize(
    context: Context,
    *,
    command: str,
    stages: list[StageResult],
    status: str,
    exit_code: int,
    started_at: str,
    release: dict[str, Any] | None,
    error: str | None,
    metadata: dict[str, Any] | None = None,
    echo: Any = print,
) -> dict[str, Any]:
    finished = utc_now()
    started = context.started_at
    result = RunResult(
        context=context,
        command=command,
        stages=stages,
        status=status,
        exit_code=exit_code,
        started_at=isoformat(started),
        finished_at=isoformat(finished),
        duration_seconds=max(0.0, (finished - started).total_seconds()),
        release=release,
        error=error,
        metadata=metadata or {},
    )
    document = write_result(result, echo=echo)
    ensure_dir(context.paths.results_dir)
    prune_runs(context, context.config.policy.keep_runs)
    return document
