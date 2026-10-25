"""Artifact validation: integrity, archive safety, secrets and smoke tests."""

from __future__ import annotations

import dataclasses
import shutil
import tempfile
import time
from pathlib import Path, PurePosixPath

from ..config import StageConfig
from ..context import Context
from ..gates import GateResult, StageResult, failed, passed, skipped, stage_result
from ..security import scan_files
from ..supplychain import load_manifest, verify_checksums
from ..util import (
    archive_kind,
    extract_archive,
    file_sha256,
    is_within,
    iter_files,
    relpath,
    sha256_file,
)
from .base import Echo, disabled_gate, run_stage_commands


def _manifest_for_run(context: Context) -> tuple[dict[str, object], Path] | None:
    for name, kind in (("package-manifest.json", "package"), ("build-manifest.json", "build")):
        path = context.paths.artifacts_dir / name
        if path.is_file():
            manifest = load_manifest(path)
            if manifest.get("kind") != kind:
                raise ValueError(f"manifest kind mismatch in {path}")
            return manifest, path
    return None


def _verify_descriptors(
    context: Context, manifest: dict[str, object]
) -> tuple[list[Path], list[str]]:
    files: list[Path] = []
    errors: list[str] = []
    artifacts = manifest.get("artifacts", [])
    if not isinstance(artifacts, list):
        return files, ["manifest.artifacts is not a list"]
    for descriptor in artifacts:
        if not isinstance(descriptor, dict):
            errors.append("manifest contains a malformed artifact descriptor")
            continue
        relative = descriptor.get("path")
        expected_hash = descriptor.get("sha256")
        expected_size = descriptor.get("size")
        if not isinstance(relative, str):
            errors.append("artifact descriptor has no path")
            continue
        path = (context.paths.artifacts_dir / relative).resolve()
        if not is_within(path, context.paths.artifacts_dir):
            errors.append(f"artifact path escapes run directory: {relative}")
            continue
        if not path.is_file():
            errors.append(f"missing artifact: {relative}")
            continue
        digest, size = file_sha256(path)
        if isinstance(expected_hash, str) and digest != expected_hash:
            errors.append(f"sha256 mismatch: {relative}")
        if isinstance(expected_size, int) and size != expected_size:
            errors.append(f"size mismatch: {relative}")
        files.append(path)
    return files, errors


def _scan_extracted(
    *,
    root: Path,
    allowlist: list[str],
    label: str,
) -> tuple[list[dict[str, object]], int, int]:
    files = list(iter_files(root))
    report = scan_files(root, files, allowlist=allowlist)
    findings = []
    for finding in report.findings:
        item = finding.to_dict()
        item["path"] = f"{label}:{finding.path}"
        findings.append(item)
    return findings, report.files_scanned, len(report.allowed)


def run_artifact_stage(context: Context, echo: Echo) -> StageResult:
    started = time.monotonic()
    stage = context.config.stage("artifact") or StageConfig(
        name="artifact",
        required=False,
        timeout_seconds=900,
    )
    if not stage.enabled:
        return stage_result(
            "artifact",
            [disabled_gate(stage=stage)],
            duration_seconds=time.monotonic() - started,
            skipped_reason="disabled",
        )
    try:
        found = _manifest_for_run(context)
    except Exception as exc:
        return stage_result(
            "artifact",
            [
                failed(
                    "artifact.manifest",
                    "artifact",
                    "artifact manifest",
                    required=stage.required,
                    summary=str(exc),
                )
            ],
            duration_seconds=time.monotonic() - started,
        )
    if found is None:
        if stage.required:
            return stage_result(
                "artifact",
                [
                    failed(
                        "artifact.manifest",
                        "artifact",
                        "artifact manifest",
                        required=True,
                        summary="no build or package manifest exists for this run",
                        remediation=(
                            "Configure build outputs and/or a package stage before artifact "
                            "validation. A pass without an artifact is a false pass."
                        ),
                    )
                ],
                duration_seconds=time.monotonic() - started,
            )
        return stage_result(
            "artifact",
            [
                skipped(
                    "artifact.manifest",
                    "artifact",
                    "artifact manifest",
                    summary="no artifact was produced in this run",
                )
            ],
            duration_seconds=time.monotonic() - started,
            skipped_reason="no artifact",
        )
    manifest, manifest_path = found
    gates: list[GateResult] = [
        passed(
            "artifact.manifest",
            "artifact",
            "artifact manifest",
            required=stage.required,
            summary=(
                f"{manifest.get('kind')} manifest for {manifest.get('project')} "
                f"{manifest.get('version')}"
            ),
            artifacts=[relpath(manifest_path, context.root)],
        )
    ]

    files, descriptor_errors = _verify_descriptors(context, manifest)
    checksums_name = manifest.get("checksums")
    checksum_errors: list[str] = []
    if isinstance(checksums_name, str):
        checksum_errors = verify_checksums(
            context.paths.artifacts_dir / checksums_name,
            context.paths.artifacts_dir,
        )
    errors = [*descriptor_errors, *checksum_errors]
    if errors:
        gates.append(
            failed(
                "artifact.integrity",
                "artifact",
                "artifact integrity",
                required=stage.required,
                summary="; ".join(errors[:8]),
                details={"errors": errors},
                remediation="Do not release or deploy an artifact whose checksums do not match.",
            )
        )
    else:
        gates.append(
            passed(
                "artifact.integrity",
                "artifact",
                "artifact integrity",
                required=stage.required,
                summary=f"{len(files)} file(s); SHA-256 verified",
                details={
                    "files": [
                        {
                            "path": relpath(path, context.paths.artifacts_dir),
                            "sha256": sha256_file(path),
                            "size": path.stat().st_size,
                        }
                        for path in files
                    ]
                },
            )
        )

    extraction_root = Path(tempfile.mkdtemp(prefix="artifact-", dir=context.paths.tmp_dir))
    extracted_by_archive: dict[str, Path] = {}
    archive_errors: list[str] = []
    secret_findings: list[dict[str, object]] = []
    scanned = 0
    allowlisted = 0
    try:
        if stage.verify_archives:
            for path in files:
                if archive_kind(path) is None:
                    continue
                destination = extraction_root / path.name
                try:
                    extract_archive(path, destination)
                except Exception as exc:
                    archive_errors.append(f"{path.name}: {exc}")
                    continue
                extracted_by_archive[path.name] = destination
            if archive_errors:
                gates.append(
                    failed(
                        "artifact.archives",
                        "artifact",
                        "archive safety",
                        required=stage.required,
                        summary="; ".join(archive_errors[:5]),
                        details={"errors": archive_errors},
                        remediation=(
                            "Rebuild the archive; do not publish malformed or linked content."
                        ),
                    )
                )
            else:
                gates.append(
                    passed(
                        "artifact.archives",
                        "artifact",
                        "archive safety",
                        required=stage.required,
                        summary=(
                            f"{len(extracted_by_archive)} archive(s) extracted without traversal, "
                            "links, or corruption"
                            if extracted_by_archive
                            else "no archives in this artifact set"
                        ),
                    )
                )
        else:
            gates.append(
                skipped(
                    "artifact.archives",
                    "artifact",
                    "archive safety",
                    summary="verify_archives=false",
                )
            )

        if stage.must_contain:
            missing: list[str] = []
            for expected in stage.must_contain:
                expected_path = PurePosixPath(expected)
                if not any(
                    (root / Path(*expected_path.parts)).exists()
                    for root in extracted_by_archive.values()
                ):
                    missing.append(expected)
            if missing:
                gates.append(
                    failed(
                        "artifact.contents",
                        "artifact",
                        "required artifact contents",
                        required=stage.required,
                        summary=f"missing: {', '.join(missing)}",
                    )
                )
            else:
                gates.append(
                    passed(
                        "artifact.contents",
                        "artifact",
                        "required artifact contents",
                        required=stage.required,
                        summary=f"{len(stage.must_contain)} required path(s) present",
                    )
                )

        if stage.scan_secrets:
            for name, root in extracted_by_archive.items():
                findings, files_scanned, allowed_count = _scan_extracted(
                    root=root,
                    allowlist=stage.allowlist,
                    label=name,
                )
                secret_findings.extend(findings)
                scanned += files_scanned
                allowlisted += allowed_count
            if secret_findings:
                gates.append(
                    failed(
                        "artifact.secrets",
                        "artifact",
                        "secrets in artifact",
                        required=stage.required,
                        summary=f"{len(secret_findings)} finding(s) in extracted artifact content",
                        details={"findings": secret_findings[:100], "files_scanned": scanned},
                        remediation=(
                            "Remove the file or value from the build input; rotate the credential "
                            "if it was real. Artifacts are the last gate before release/deploy."
                        ),
                    )
                )
            else:
                gates.append(
                    passed(
                        "artifact.secrets",
                        "artifact",
                        "secrets in artifact",
                        required=stage.required,
                        summary=(
                            f"no findings in {scanned} extracted file(s)"
                            f" ({allowlisted} allowlisted)"
                            if extracted_by_archive
                            else "no archives to scan"
                        ),
                    )
                )
        else:
            gates.append(
                skipped(
                    "artifact.secrets",
                    "artifact",
                    "secrets in artifact",
                    summary="scan_secrets=false",
                )
            )

        if stage.smoke_commands:
            first = files[0] if files else None
            extra_env = {
                "LOCALCI_ARTIFACT_DIR": str(context.paths.artifacts_dir),
                "LOCALCI_ARTIFACT": str(first) if first else "",
                "LOCALCI_ARTIFACT_EXTRACTED": str(
                    next(iter(extracted_by_archive.values()))
                    if extracted_by_archive
                    else extraction_root
                ),
                "LOCALCI_RELEASE_VERSION": context.version,
            }
            smoke_stage = dataclasses.replace(
                stage,
                commands=list(stage.smoke_commands),
                fix_commands=[],
                timeout_seconds=stage.timeout_seconds,
            )
            smoke_gates, _ = run_stage_commands(
                context=context,
                stage=smoke_stage,
                echo=echo,
                gate_stage="artifact",
                extra_env=extra_env,
            )
            for index, gate in enumerate(smoke_gates, start=1):
                gate.id = f"artifact.smoke.{index:02d}"
                gate.title = f"artifact smoke: {gate.command or gate.title}"
                gates.append(gate)
        else:
            gates.append(
                skipped(
                    "artifact.smoke",
                    "artifact",
                    "artifact smoke tests",
                    summary="no smoke_commands configured",
                )
            )
    finally:
        shutil.rmtree(extraction_root, ignore_errors=True)

    return stage_result("artifact", gates, duration_seconds=time.monotonic() - started)
