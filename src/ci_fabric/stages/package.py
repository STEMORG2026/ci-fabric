"""Package stage: deterministic archive or repository-provided packaging command."""

from __future__ import annotations

import glob
import shutil
import time
from pathlib import Path

from ..config import DEFAULT_PACKAGE_EXCLUDES
from ..context import Context
from ..gates import GateResult, StageResult, failed, passed, skipped, stage_result, warning
from ..supplychain import (
    build_provenance,
    build_sbom,
    create_archive,
    media_type,
    verify_checksums,
    write_checksums,
    write_manifest,
)
from ..util import ensure_dir, is_within, relpath
from .base import Echo, disabled_gate, disabled_skip_reason, run_stage_commands


def _expand(context: Context, patterns: list[str]) -> list[Path]:
    matches: list[Path] = []
    seen: set[Path] = set()
    for pattern in patterns:
        rendered = context.render(pattern)
        candidate = Path(rendered)
        if not candidate.is_absolute():
            candidate = context.root / candidate
        for item in glob.glob(str(candidate), recursive=True):
            path = Path(item).resolve()
            if path in seen or not is_within(path, context.root):
                continue
            seen.add(path)
            matches.append(path)
    return sorted(matches)


def _collect_command_outputs(context: Context, patterns: list[str]) -> tuple[list[Path], list[str]]:
    package_dir = ensure_dir(context.paths.artifacts_dir / "package")
    collected: list[Path] = []
    problems: list[str] = []
    artifacts_root = context.paths.artifacts_dir.resolve()
    for source in _expand(context, patterns):
        if is_within(source, artifacts_root):
            if source.is_file():
                collected.append(source)
            else:
                collected.extend(path for path in sorted(source.rglob("*")) if path.is_file())
            continue
        destination = package_dir / source.name
        if destination.exists():
            problems.append(f"duplicate package output name: {source.name}")
            continue
        if source.is_dir():
            shutil.copytree(source, destination, symlinks=True)
            collected.extend(path for path in sorted(destination.rglob("*")) if path.is_file())
        else:
            ensure_dir(destination.parent)
            shutil.copy2(source, destination)
            collected.append(destination)
    return sorted(set(collected)), problems


def _archive_output(context: Context, stage_output: str | None) -> Path:
    if stage_output:
        candidate = Path(context.render(stage_output))
        if not candidate.is_absolute():
            candidate = context.root / candidate
        return candidate.resolve()
    suffix = ".tar.gz"
    return context.paths.artifacts_dir / f"{context.config.project.name}-{context.version}{suffix}"


def run_package_stage(context: Context, echo: Echo) -> StageResult:
    started = time.monotonic()
    stage = context.config.stage("package")
    if stage is None:
        return stage_result(
            "package",
            [
                skipped(
                    "package.config",
                    "package",
                    "package stage",
                    summary="[stages.package] is not configured",
                )
            ],
            duration_seconds=time.monotonic() - started,
            skipped_reason="not configured",
        )
    if not stage.enabled:
        return stage_result(
            "package",
            [disabled_gate(stage=stage)],
            duration_seconds=time.monotonic() - started,
            skipped_reason=disabled_skip_reason(stage),
        )

    gates: list[GateResult] = []
    package_files: list[Path] = []
    commands = [context.render(command) for command in stage.commands]

    if stage.strategy == "archive":
        build_stage = context.config.stage("build")
        inputs = stage.inputs or (build_stage.outputs if build_stage else [])
        expanded = _expand(context, inputs) if inputs else []
        if not expanded:
            gates.append(
                failed(
                    "package.inputs",
                    "package",
                    "package inputs",
                    required=stage.required,
                    summary=(
                        "no package inputs found; declare [stages.package].inputs or a "
                        "build stage with outputs"
                    ),
                    remediation="Package the exact files produced and validated by the build.",
                )
            )
            return stage_result("package", gates, duration_seconds=time.monotonic() - started)
        output = _archive_output(context, stage.output)
        try:
            archive = create_archive(
                root=context.root,
                inputs=expanded,
                output=output,
                fmt=stage.archive_format,
                epoch=context.source_date_epoch,
                excludes=[*DEFAULT_PACKAGE_EXCLUDES, *stage.exclude],
                strip_prefix=stage.strip_prefix,
            )
        except Exception as exc:
            gates.append(
                failed(
                    "package.archive",
                    "package",
                    "deterministic package archive",
                    required=stage.required,
                    summary=f"could not create archive: {exc}",
                    remediation=(
                        "Check inputs, strip_prefix, exclude patterns, and write permissions."
                    ),
                )
            )
            return stage_result("package", gates, duration_seconds=time.monotonic() - started)
        if not is_within(archive.path, context.paths.artifacts_dir):
            destination = context.paths.artifacts_dir / archive.path.name
            ensure_dir(destination.parent)
            shutil.copy2(archive.path, destination)
            package_files.append(destination)
        else:
            package_files.append(archive.path)
        gates.append(
            passed(
                "package.archive",
                "package",
                "deterministic package archive",
                required=stage.required,
                summary=(
                    f"{len(archive.entries)} entries, {archive.size} bytes, "
                    f"sha256 {archive.sha256[:16]}..."
                ),
                details={
                    "entries": archive.entries[:200],
                    "sha256": archive.sha256,
                    "size": archive.size,
                    "format": stage.archive_format,
                    "strip_prefix": stage.strip_prefix,
                },
                artifacts=[relpath(path, context.root) for path in package_files],
            )
        )
    elif stage.strategy == "command":
        if not stage.commands:
            gates.append(
                failed(
                    "package.command",
                    "package",
                    "package command",
                    required=stage.required,
                    summary="package.strategy=command has no commands",
                )
            )
            return stage_result("package", gates, duration_seconds=time.monotonic() - started)
        command_gates, _ = run_stage_commands(context=context, stage=stage, echo=echo)
        for index, gate in enumerate(command_gates, start=1):
            gate.id = f"package.command.{index:02d}"
            gates.append(gate)
        if any(gate.blocking_failure for gate in gates):
            return stage_result("package", gates, duration_seconds=time.monotonic() - started)
        if not stage.outputs:
            gates.append(
                failed(
                    "package.outputs",
                    "package",
                    "package outputs",
                    required=stage.required,
                    summary="package.strategy=command must declare outputs",
                )
            )
            return stage_result("package", gates, duration_seconds=time.monotonic() - started)
        package_files, problems = _collect_command_outputs(context, stage.outputs)
        if problems:
            gates.append(
                failed(
                    "package.outputs",
                    "package",
                    "package outputs",
                    required=stage.required,
                    summary="; ".join(problems),
                )
            )
        elif not package_files:
            gates.append(
                failed(
                    "package.outputs",
                    "package",
                    "package outputs",
                    required=stage.required,
                    summary="no package outputs matched",
                )
            )
        else:
            gates.append(
                passed(
                    "package.outputs",
                    "package",
                    "package outputs",
                    required=stage.required,
                    summary=f"{len(package_files)} package file(s)",
                    artifacts=[relpath(path, context.root) for path in package_files],
                )
            )
    else:  # pragma: no cover - config validation
        gates.append(
            failed(
                "package.strategy",
                "package",
                "package strategy",
                required=stage.required,
                summary=f"unsupported strategy: {stage.strategy}",
            )
        )
        return stage_result("package", gates, duration_seconds=time.monotonic() - started)

    if not package_files:
        return stage_result("package", gates, duration_seconds=time.monotonic() - started)
    for path in package_files:
        if not is_within(path, context.paths.artifacts_dir):
            raise RuntimeError(f"internal error: package file outside artifacts: {path}")

    sbom, sbom_warnings, sbom_path = build_sbom(
        config=context.config,
        context=context,
        artifacts=package_files,
    )
    component_count = len(sbom.get("components", []))
    sbom_gate = passed(
        "supplychain.sbom",
        "package",
        "CycloneDX SBOM",
        required=stage.required,
        summary=f"{component_count} component(s)",
        details={"warnings": sbom_warnings},
        artifacts=[relpath(sbom_path, context.root)],
    )
    if not component_count:
        sbom_gate = warning(
            "supplychain.sbom",
            "package",
            "CycloneDX SBOM",
            summary="SBOM generated but no pinned dependencies were found",
            remediation=(
                "Add a supported lockfile (uv.lock, requirements*.txt with ==, "
                "package-lock.json, pnpm-lock.yaml, Cargo.lock, or go.sum)."
            ),
        )
    gates.append(sbom_gate)

    build_stage = context.config.stage("build")
    build_commands = list(build_stage.commands) if build_stage is not None else []
    provenance_commands = commands or [context.render(command) for command in build_commands]
    provenance, provenance_path = build_provenance(
        config=context.config,
        context=context,
        artifacts=package_files,
        sbom_path=sbom_path,
        commands=provenance_commands,
    )
    gates.append(
        passed(
            "supplychain.provenance",
            "package",
            "build provenance",
            required=stage.required,
            summary=(f"{len(provenance.get('subject', []))} subject(s); unsigned local provenance"),
            artifacts=[relpath(provenance_path, context.root)],
        )
    )

    checksums_name = "SHA256SUMS"
    manifest, manifest_path = write_manifest(
        kind="package",
        config=context.config,
        context=context,
        artifacts=package_files,
        extra={
            "sbom": relpath(sbom_path, context.paths.artifacts_dir),
            "provenance": relpath(provenance_path, context.paths.artifacts_dir),
            "checksums": checksums_name,
        },
    )
    checksums_path = context.paths.artifacts_dir / checksums_name
    write_checksums(
        [*package_files, sbom_path, provenance_path, manifest_path],
        checksums_path,
    )
    problems = verify_checksums(checksums_path, context.paths.artifacts_dir)
    gates.append(
        passed(
            "package.checksums",
            "package",
            "artifact checksums",
            required=stage.required,
            summary=f"{len(manifest['artifacts'])} artifact(s) verified",
            artifacts=[relpath(checksums_path, context.root)],
        )
        if not problems
        else failed(
            "package.checksums",
            "package",
            "artifact checksums",
            required=stage.required,
            summary="; ".join(problems[:5]),
            details={"problems": problems},
        )
    )
    for gate in gates:
        if gate.id == "package.checksums" and not problems:
            gate.details = {"media_types": {path.name: media_type(path) for path in package_files}}
    return stage_result("package", gates, duration_seconds=time.monotonic() - started)
