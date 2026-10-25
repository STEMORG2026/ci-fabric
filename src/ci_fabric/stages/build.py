"""Deterministic build stage and build metadata."""

from __future__ import annotations

import glob
import shutil
import time
from pathlib import Path

from ..context import Context
from ..gates import StageResult, failed, passed, skipped, stage_result, warning
from ..supplychain import write_manifest
from ..util import ensure_dir, is_within, relpath, write_json
from .base import Echo, disabled_gate, no_command_gate, run_stage_commands


def _expand_outputs(context: Context, patterns: list[str]) -> list[Path]:
    matches: list[Path] = []
    seen: set[Path] = set()
    for raw in patterns:
        rendered = context.render(raw)
        candidate = Path(rendered)
        if not candidate.is_absolute():
            candidate = context.root / candidate
        pattern = str(candidate)
        for match in glob.glob(pattern, recursive=True):
            path = Path(match).resolve()
            if not is_within(path, context.root):
                continue
            if path in seen:
                continue
            seen.add(path)
            matches.append(path)
    return sorted(matches)


def _collect_build_outputs(context: Context, outputs: list[Path]) -> tuple[list[Path], list[str]]:
    destination_root = ensure_dir(context.paths.artifacts_dir / "build")
    collected: list[Path] = []
    problems: list[str] = []
    for source in outputs:
        if not source.exists():
            problems.append(f"output does not exist: {source}")
            continue
        try:
            relative = source.relative_to(context.root)
        except ValueError:
            problems.append(f"output is outside the repository: {source}")
            continue
        destination = destination_root / relative
        if source.is_dir():
            if destination.exists():
                shutil.rmtree(destination)
            shutil.copytree(source, destination, symlinks=True)
            collected.extend(child for child in sorted(destination.rglob("*")) if child.is_file())
        else:
            ensure_dir(destination.parent)
            shutil.copy2(source, destination)
            collected.append(destination)
    return sorted(set(collected)), problems


def run_build_stage(context: Context, echo: Echo) -> StageResult:
    started = time.monotonic()
    stage = context.config.stage("build")
    if stage is None:
        return stage_result(
            "build",
            [
                skipped(
                    "build.config",
                    "build",
                    "build command",
                    summary="[stages.build] is not configured",
                )
            ],
            duration_seconds=time.monotonic() - started,
            skipped_reason="not configured",
        )
    if not stage.enabled:
        return stage_result(
            "build",
            [disabled_gate(stage=stage)],
            duration_seconds=time.monotonic() - started,
            skipped_reason="disabled",
        )
    if not stage.commands:
        return stage_result(
            "build",
            [
                no_command_gate(
                    stage=stage,
                    context=context,
                    title="build command",
                    purpose="build stage",
                )
            ],
            duration_seconds=time.monotonic() - started,
            skipped_reason=None if stage.required else "not configured",
        )

    gates, results = run_stage_commands(context=context, stage=stage, echo=echo)
    if any(gate.blocking_failure for gate in gates):
        return stage_result("build", gates, duration_seconds=time.monotonic() - started)

    build_files: list[Path] = []
    output_problems: list[str] = []
    if stage.outputs:
        expanded = _expand_outputs(context, stage.outputs)
        if not expanded:
            gates.append(
                failed(
                    "build.outputs",
                    "build",
                    "build outputs",
                    required=stage.required,
                    summary=f"no files matched outputs: {', '.join(stage.outputs)}",
                    remediation=(
                        "Fix [stages.build].outputs. A build that produces no artifact cannot "
                        "be validated, released, or deployed."
                    ),
                )
            )
        else:
            build_files, output_problems = _collect_build_outputs(context, expanded)
            if output_problems:
                gates.append(
                    failed(
                        "build.outputs",
                        "build",
                        "build outputs",
                        required=stage.required,
                        summary="; ".join(output_problems[:5]),
                        details={"problems": output_problems},
                    )
                )
            elif not build_files:
                gates.append(
                    failed(
                        "build.outputs",
                        "build",
                        "build outputs",
                        required=stage.required,
                        summary="build outputs were found but contained no files",
                    )
                )
            else:
                gates.append(
                    passed(
                        "build.outputs",
                        "build",
                        "build outputs",
                        required=stage.required,
                        summary=f"{len(build_files)} file(s) collected",
                        artifacts=[relpath(path, context.root) for path in build_files[:50]],
                    )
                )
    else:
        gates.append(
            warning(
                "build.outputs",
                "build",
                "build outputs",
                summary="no outputs declared; build success cannot be validated as an artifact",
                remediation='Declare [stages.build].outputs (for example ["dist"]).',
            )
        )

    if build_files:
        manifest, manifest_path = write_manifest(
            kind="build",
            config=context.config,
            context=context,
            artifacts=build_files,
        )
        build_info = {
            "schema_version": 1,
            "project": context.config.project.name,
            "version": context.config.project.version,
            "revision": context.git.revision,
            "short_revision": context.git.short_revision,
            "dirty": context.git.dirty,
            "run_id": context.run_id,
            "source_date_epoch": context.source_date_epoch,
            "commands": [context.render(command) for command in stage.commands],
            "tool_versions": context.tool_versions,
            "build_files": len(manifest["artifacts"]),
            "manifest": relpath(manifest_path, context.root),
        }
        info_path = context.paths.artifacts_dir / "build-info.json"
        write_json(info_path, build_info)
        gates.append(
            passed(
                "build.metadata",
                "build",
                "build metadata",
                required=stage.required,
                summary=(
                    f"{len(manifest['artifacts'])} artifact(s); "
                    f"revision {context.git.short_revision or 'nogit'}"
                ),
                artifacts=[
                    relpath(manifest_path, context.root),
                    relpath(info_path, context.root),
                ],
            )
        )
    elif stage.outputs:
        gates.append(
            failed(
                "build.metadata",
                "build",
                "build metadata",
                required=stage.required,
                summary="no build files to manifest",
            )
        )

    duration = time.monotonic() - started
    for gate in gates:
        if gate.duration_seconds == 0.0 and gate.id.startswith("build."):
            gate.duration_seconds = results[0].duration_seconds if results else 0.0
    return stage_result("build", gates, duration_seconds=duration)
