"""Deterministic build stage and build metadata."""

from __future__ import annotations

import glob
import shutil
import time
from pathlib import Path

from .. import buildcache
from ..config import StageConfig
from ..context import Context
from ..gates import GateResult, StageResult, failed, passed, skipped, stage_result, warning
from ..supplychain import write_manifest
from ..util import ensure_dir, is_within, relpath, tracked_files, write_json
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


def _refusal_reason(context: Context, path: Path) -> str | None:
    """Why *path* must never be cleaned, or None when it may be."""
    root = context.root.resolve()
    if path == root:
        return "the repository root"
    if not is_within(path, root):
        return "outside the repository"
    for protected in (context.paths.state_dir, root / ".localci"):
        if is_within(path, protected):
            return f"{relpath(protected, root)} (ci-fabric's own state)"
    return None


def _clean_declared_outputs(context: Context, stage: StageConfig, echo: Echo) -> list[GateResult]:
    """Remove previously declared build outputs before the build runs.

    Declared outputs are what *this* revision's build produces, so anything
    already sitting there belongs to an earlier build. Most toolchains append
    (`python -m build --outdir dist` is the canonical example): left alone,
    every historical distribution stays in the output directory and is then
    packaged, checksummed and released as part of the new bundle — a bundle
    whose contents, and therefore whose digest, depend on the machine's build
    history instead of on the commit. A fresh clone and a long-lived checkout
    of the same revision must produce the same bytes, so the output directory
    is emptied first.

    Deletion is deliberately conservative: the repository root, anything
    outside the repository, and ci-fabric's own state directories (`.ci`,
    `.localci`) are refused as configuration errors rather than deleted, and a
    path tracked in Git is reported and left alone — a committed file is a
    source, never a build product.
    """
    started = time.monotonic()
    declared = list(stage.outputs)
    tracked = set(tracked_files(context.root)) if context.git.available else set()
    removed: list[str] = []
    left_tracked: list[str] = []
    refused: list[str] = []
    for path in _expand_outputs(context, declared):
        relative = relpath(path, context.root)
        reason = _refusal_reason(context, path)
        if reason is not None:
            refused.append(f"{relative} is {reason}")
            continue
        if relative in tracked:
            left_tracked.append(relative)
            continue
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError as exc:
            refused.append(f"{relative} could not be removed: {exc.strerror or exc}")
            continue
        removed.append(relative)

    duration = time.monotonic() - started
    if refused:
        return [
            failed(
                "build.clean",
                "build",
                "stale build outputs",
                required=stage.required,
                summary="; ".join(refused[:5]),
                remediation=(
                    "Fix [stages.build].outputs: declared outputs must be build products "
                    "inside the repository, never the checkout root, the tool's own state "
                    "directories (.ci, .localci), or an unremovable path."
                ),
                duration_seconds=duration,
            )
        ]

    gates = [
        passed(
            "build.clean",
            "build",
            "stale build outputs",
            required=stage.required,
            summary=(
                f"removed {len(removed)} stale output path(s) before the build"
                if removed
                else "outputs were already clean"
            ),
            details={"removed": removed, "declared": declared},
            duration_seconds=duration,
        )
    ]
    if left_tracked:
        gates.append(
            warning(
                "build.clean.tracked",
                "build",
                "tracked output paths",
                summary=f"left in place (tracked in Git): {', '.join(left_tracked[:5])}",
                remediation=(
                    "Declared build outputs should be generated files. A tracked path is "
                    "never deleted by the build stage."
                ),
                details={"tracked": left_tracked},
                duration_seconds=duration,
            )
        )
    return gates


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

    rendered_commands = [context.render(command) for command in stage.commands]

    # Declared outputs are emptied first: the bundle must be what this commit
    # builds, never what this machine built before. (See _clean_declared_outputs.)
    clean_gates: list[GateResult] = (
        _clean_declared_outputs(context, stage, echo) if stage.outputs else []
    )
    if any(gate.blocking_failure for gate in clean_gates):
        # A refused cleanup means the tree cannot be trusted to hold only this
        # build's outputs; building anyway would package whatever is there.
        return stage_result("build", clean_gates, duration_seconds=time.monotonic() - started)

    # Content-addressed build cache (opt-in). A clean tree at the same
    # revision with identical rendered commands restores the previous build's
    # outputs instead of re-running the build; a dirty tree is never looked
    # up and never stored. Only green builds are cached, and restores are
    # digest-verified, so a hit is always the artifact the key promises.
    cache_gates: list[GateResult] = []
    if stage.cache:
        if not context.git.available:
            cache_gates.append(
                warning(
                    "build.cache",
                    "build",
                    "build cache",
                    summary="cache=true requires a Git repository; building normally",
                )
            )
        elif context.git.dirty:
            cache_gates.append(
                warning(
                    "build.cache",
                    "build",
                    "build cache",
                    summary=(
                        "tree is dirty; cache not consulted (a dirty build is not reproducible)"
                    ),
                )
            )
        else:
            key = buildcache.cache_key(context, rendered_commands)
            entry = buildcache.lookup(context, key)
            if entry is not None:
                restored = buildcache.restore(entry, context)
                echo(f"  cache hit {key[:12]}: restored {len(restored)} output(s); build skipped")
                cache_gates.append(
                    passed(
                        "build.cache",
                        "build",
                        "build cache",
                        required=stage.required,
                        summary=(
                            f"cache hit {key[:12]}: {len(restored)} output(s) restored, "
                            "build commands skipped"
                        ),
                        details={"key": key, "restored": restored[:50]},
                    )
                )
                return _finish_cached_build(
                    context,
                    stage,
                    [*clean_gates, *cache_gates],
                    started,
                    rendered_commands,
                )
            echo(f"  cache miss {key[:12]}: building and caching")

    gates, results = run_stage_commands(
        context=context, stage=stage, echo=echo, commands=stage.commands
    )
    gates = [*clean_gates, *cache_gates, *gates]
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

    # A green build with valid, declared outputs is worth caching.
    if (
        stage.cache
        and context.git.available
        and not context.git.dirty
        and not any(gate.failed for gate in gates)
        and build_files
    ):
        key = buildcache.cache_key(context, rendered_commands)
        stored = buildcache.store(context, key, rendered_commands, expanded or [])
        if stored is not None:
            gates.append(
                passed(
                    "build.cache",
                    "build",
                    "build cache",
                    required=stage.required,
                    summary=f"cached under {key[:12]} for clean-tree reuse",
                    details={"key": key},
                    artifacts=[relpath(stored, context.root)],
                )
            )
    return stage_result("build", gates, duration_seconds=duration)


def _finish_cached_build(
    context: Context,
    stage: StageConfig,
    gates: list[GateResult],
    started: float,
    rendered_commands: list[str],
) -> StageResult:
    """Validate restored outputs and write build metadata, as a fresh build would."""
    expanded = _expand_outputs(context, stage.outputs) if stage.outputs else []
    build_files: list[Path] = []
    if expanded:
        build_files, problems = _collect_build_outputs(context, expanded)
        if problems:
            gates.append(
                failed(
                    "build.outputs",
                    "build",
                    "build outputs",
                    required=stage.required,
                    summary="; ".join(problems[:5]),
                    details={"problems": problems},
                )
            )
        else:
            gates.append(
                passed(
                    "build.outputs",
                    "build",
                    "build outputs",
                    required=stage.required,
                    summary=f"{len(build_files)} file(s) collected (restored from cache)",
                    artifacts=[relpath(path, context.root) for path in build_files[:50]],
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
            "commands": rendered_commands,
            "tool_versions": context.tool_versions,
            "build_files": len(manifest["artifacts"]),
            "manifest": relpath(manifest_path, context.root),
            "cache_hit": True,
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
                    f"revision {context.git.short_revision or 'nogit'} (cache hit)"
                ),
                artifacts=[
                    relpath(manifest_path, context.root),
                    relpath(info_path, context.root),
                ],
            )
        )
    return stage_result("build", gates, duration_seconds=time.monotonic() - started)
