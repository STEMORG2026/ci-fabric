"""Secrets, configuration security and SAST orchestration."""

from __future__ import annotations

import time

from ..config import StageConfig
from ..context import Context
from ..gates import GateResult, StageResult, failed, passed, skipped, stage_result
from ..security import deploy_config_findings, scan_repository
from ..util import git_is_ignored, relpath, write_json
from .base import Echo, disabled_gate, run_stage_commands


def _security_stage(context: Context) -> StageConfig:
    return context.config.stage("security") or StageConfig(
        name="security",
        required=True,
        timeout_seconds=600,
    )


def run_security_stage(
    context: Context,
    echo: Echo,
    *,
    secrets_only: bool = False,
) -> StageResult:
    started = time.monotonic()
    stage = _security_stage(context)
    gates: list[GateResult] = []
    if not stage.enabled:
        return stage_result(
            "security",
            [disabled_gate(stage=stage)],
            duration_seconds=time.monotonic() - started,
            skipped_reason="disabled",
        )

    if stage.secret_scan:
        echo("  scanning tracked files for credential-shaped strings")
        report = scan_repository(
            context.root,
            include_untracked=stage.scan_untracked,
            allowlist=stage.allowlist,
            exclude_globs=stage.exclude,
        )
        report_path = context.paths.reports_dir / "secrets.json"
        write_json(report_path, report.to_dict())
        details = {
            "files_scanned": report.files_scanned,
            "files_skipped": report.files_skipped,
            "allowed_findings": len(report.allowed),
            "findings": [finding.to_dict() for finding in report.findings[:100]],
            "skipped_large": report.skipped_large[:100],
        }
        if report.findings:
            gates.append(
                failed(
                    "security.secrets",
                    "security",
                    "secret scan",
                    required=stage.required,
                    summary=(
                        f"{len(report.findings)} potential secret(s) in "
                        f"{report.files_scanned} files"
                    ),
                    log=relpath(report_path, context.root),
                    details=details,
                    remediation=(
                        "Remove the credential and rotate it. For deliberate test fixtures, add "
                        "`localci:allow-secret` on the line or configure a precise allowlist entry "
                        "such as `tests/fixtures/**/*.py:generic-assignment`."
                    ),
                )
            )
        else:
            gates.append(
                passed(
                    "security.secrets",
                    "security",
                    "secret scan",
                    required=stage.required,
                    summary=(
                        f"no findings in {report.files_scanned} files "
                        f"({len(report.allowed)} allowlisted)"
                    ),
                    log=relpath(report_path, context.root),
                    details=details,
                )
            )
    else:
        gates.append(
            skipped(
                "security.secrets",
                "security",
                "secret scan",
                summary="[stages.security].secret_scan=false",
            )
        )

    if stage.scan_config:
        config_problems = deploy_config_findings(context.config)
        if context.git.available:
            for state_path in (".ci/", ".localci/"):
                if not git_is_ignored(context.root, state_path):
                    config_problems.append(f"{state_path} is not ignored by Git")
        if config_problems:
            gates.append(
                failed(
                    "security.config",
                    "security",
                    "configuration security",
                    required=stage.required,
                    summary="; ".join(config_problems[:10]),
                    details={"problems": config_problems},
                    remediation=(
                        "Reference secrets as ${NAME} from the deploy environment and ignore "
                        ".ci/ plus .localci/ in Git."
                    ),
                )
            )
        else:
            gates.append(
                passed(
                    "security.config",
                    "security",
                    "configuration security",
                    required=stage.required,
                    summary="no literal deploy secrets; state directories ignored",
                )
            )
    else:
        gates.append(
            skipped(
                "security.config",
                "security",
                "configuration security",
                summary="[stages.security].scan_config=false",
            )
        )

    if secrets_only:
        gates.append(
            skipped(
                "security.sast",
                "security",
                "SAST commands",
                summary="secrets-only invocation",
            )
        )
    elif stage.commands:
        command_gates, _ = run_stage_commands(
            context=context,
            stage=stage,
            echo=echo,
            gate_stage="security",
        )
        for index, gate in enumerate(command_gates, start=1):
            gate.id = f"security.sast.{index:02d}"
            gate.title = f"SAST: {gate.command or gate.title}"
            gates.append(gate)
    else:
        gates.append(
            skipped(
                "security.sast",
                "security",
                "SAST commands",
                summary="no SAST command configured (secrets and configuration still scanned)",
            )
        )
    return stage_result("security", gates, duration_seconds=time.monotonic() - started)
