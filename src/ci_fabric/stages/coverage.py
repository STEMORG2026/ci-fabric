"""Coverage command execution and threshold enforcement."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from ..context import Context
from ..errors import CiError
from ..gates import GateResult, StageResult, errored, failed, passed, skipped, stage_result
from ..runner import run_command
from ..util import parse_xml_file, read_text, relpath
from .base import COVERAGE_DATA_NAME, Echo, coverage_data_files, disabled_gate, disabled_skip_reason

_TOTAL_PATTERN = re.compile(r"(?i)\bTOTAL\b.*?(\d+(?:\.\d+)?)%")
_ALL_FILES_PATTERN = re.compile(r"(?i)\bAll files\b.*\|\s*(\d+(?:\.\d+)?)\s*%?")


def parse_coverage_report(path: Path) -> tuple[float | None, str]:
    suffix = path.suffix.lower()
    try:
        if suffix == ".xml":
            root = parse_xml_file(path)
            rate = root.attrib.get("line-rate")
            if rate is not None:
                return round(float(rate) * 100, 2), "line-rate"
            return None, "XML has no line-rate"
        if suffix == ".json":
            data = json.loads(read_text(path))
            if isinstance(data, dict):
                totals = data.get("totals")
                if isinstance(totals, dict):
                    value = totals.get("percent_covered")
                    if isinstance(value, (int, float)):
                        return round(float(value), 2), "percent_covered"
                value = data.get("percent_covered")
                if isinstance(value, (int, float)):
                    return round(float(value), 2), "percent_covered"
            return None, "JSON has no coverage total"
        if suffix in {".info", ".lcov"}:
            found = 0
            hit = 0
            for line in read_text(path).splitlines():
                if line.startswith("LF:"):
                    found += int(line[3:] or 0)
                elif line.startswith("LH:"):
                    hit += int(line[3:] or 0)
            if found:
                return round(hit / found * 100, 2), "lcov lines"
            return None, "lcov has no line data"
    except (OSError, ValueError, CiError, json.JSONDecodeError) as exc:
        return None, f"could not parse coverage report: {exc}"
    return None, "unsupported coverage report format"


def _coverage_from_output(output: str) -> tuple[float | None, str]:
    match = _TOTAL_PATTERN.search(output)
    if match:
        return float(match.group(1)), "TOTAL line"
    match = _ALL_FILES_PATTERN.search(output)
    if match:
        return float(match.group(1)), "All files line"
    return None, "no coverage percentage in output"


def _combine_command(context: Context) -> str:
    """The command that turns the tiers' data files into a report and XML.

    No `--fail-under` here on purpose: the command measures, the
    `coverage.threshold` gate judges. Baking the threshold into the command would
    hide the measured number behind a shell exit code and duplicate the verdict.
    """
    return context.render(
        "{python} -m coverage combine {artifacts} && "
        "{python} -m coverage report && "
        "{python} -m coverage xml -o {artifacts}/coverage.xml"
    )


def run_coverage_stage(context: Context, echo: Echo) -> StageResult:
    started = time.monotonic()
    stage = context.config.stage("coverage")
    if stage is None:
        return stage_result(
            "coverage",
            [
                skipped(
                    "coverage.config",
                    "coverage",
                    "coverage gate",
                    summary="[stages.coverage] is not configured",
                )
            ],
            duration_seconds=time.monotonic() - started,
            skipped_reason="not configured",
        )
    if not stage.enabled:
        return stage_result(
            "coverage",
            [disabled_gate(stage=stage)],
            duration_seconds=time.monotonic() - started,
            skipped_reason=disabled_skip_reason(stage),
        )
    gates: list[GateResult] = []
    # Tiers that declared measure_coverage left their data files in this run's
    # artifact directory: combine them. A configured command is the standalone
    # path (a `./ci coverage` with no measured tiers) and is used only then.
    measured = coverage_data_files(context)
    synthesized = _combine_command(context) if measured else None
    if synthesized is None and not stage.commands:
        gate = failed(
            "coverage.config",
            "coverage",
            "coverage command",
            required=stage.required,
            summary=("no test tier measured coverage in this run and no command is configured"),
            remediation=(
                "Set measure_coverage = true on a test tier (its data files are combined "
                "here), or give [stages.coverage] a command for a standalone check."
            ),
        )
        return stage_result("coverage", [gate], duration_seconds=time.monotonic() - started)

    command = synthesized or context.render(stage.commands[0])
    environment = context.stage_env(stage)
    if synthesized is not None and "COVERAGE_FILE" not in stage.env:
        environment["COVERAGE_FILE"] = str(context.paths.artifacts_dir / COVERAGE_DATA_NAME)
    log = context.paths.logs_dir / "coverage-01.log"
    echo(f"  $ {command}")
    if measured:
        echo(f"  combining {len(measured)} measured tier file(s) from this run")
    result = run_command(
        command,
        cwd=context.stage_cwd(stage),
        env=environment,
        timeout=float(stage.timeout_seconds),
        log_path=log,
        echo=echo,
    )
    relative_log = relpath(log, context.root)
    if not result.ok:
        no_verdict = result.timed_out or result.exit_code is None or result.exit_code == 127
        gates.append(
            (errored if no_verdict else failed)(
                "coverage.command",
                "coverage",
                command,
                required=stage.required,
                summary=result.summary(),
                command=command,
                exit_code=result.exit_code,
                duration_seconds=result.duration_seconds,
                log=relative_log,
                remediation="Fix the coverage command; no threshold can be verified without it.",
                details={"outcome": "unverified"} if no_verdict else {},
            )
        )
        return stage_result("coverage", gates, duration_seconds=time.monotonic() - started)
    gates.append(
        passed(
            "coverage.command",
            "coverage",
            command,
            required=stage.required,
            summary=result.summary(),
            command=command,
            exit_code=0,
            duration_seconds=result.duration_seconds,
            log=relative_log,
        )
    )

    percent: float | None = None
    source = ""
    report_path: Path | None = None
    if stage.report_path:
        report_path = context.root / context.render(stage.report_path)
        if report_path.is_file():
            percent, source = parse_coverage_report(report_path)
        else:
            source = f"report not found: {relpath(report_path, context.root)}"
    elif synthesized is not None:
        # The combine sequence wrote the XML itself; the threshold reads it.
        report_path = context.paths.artifacts_dir / "coverage.xml"
        if report_path.is_file():
            percent, source = parse_coverage_report(report_path)
        else:
            percent, source = _coverage_from_output(result.output_tail)
    else:
        percent, source = _coverage_from_output(result.output_tail)
    if percent is None and not stage.report_path:
        percent, source = _coverage_from_output(result.output_tail)

    details = {
        "percent": percent,
        "source": source,
        "report_path": relpath(report_path, context.root) if report_path else None,
        "command_output_tail": result.output_tail[-2000:],
    }
    if stage.min_percent is not None:
        if percent is None:
            gates.append(
                failed(
                    "coverage.threshold",
                    "coverage",
                    f"coverage >= {stage.min_percent}%",
                    required=True,
                    summary=f"coverage percentage unavailable ({source})",
                    log=relative_log,
                    details=details,
                    remediation=(
                        "Configure [stages.coverage].report_path to a coverage XML/JSON/lcov "
                        "file, or make the command print a TOTAL line."
                    ),
                )
            )
        elif percent + 1e-9 < stage.min_percent:
            gates.append(
                failed(
                    "coverage.threshold",
                    "coverage",
                    f"coverage >= {stage.min_percent}%",
                    required=True,
                    summary=f"{percent:.2f}% < {stage.min_percent:.2f}% ({source})",
                    log=relative_log,
                    details=details,
                    remediation=(
                        "Add tests for uncovered behavior. Do not lower the threshold without "
                        "recording the reason in the policy documentation."
                    ),
                )
            )
        else:
            gates.append(
                passed(
                    "coverage.threshold",
                    "coverage",
                    f"coverage >= {stage.min_percent}%",
                    required=True,
                    summary=f"{percent:.2f}% ({source})",
                    log=relative_log,
                    details=details,
                    artifacts=(
                        [relpath(report_path, context.root)]
                        if report_path and report_path.exists()
                        else []
                    ),
                )
            )
    else:
        gates.append(
            passed(
                "coverage.report",
                "coverage",
                "coverage report",
                required=stage.required,
                summary=(
                    f"{percent:.2f}% ({source})"
                    if percent is not None
                    else f"reported without threshold ({source})"
                ),
                log=relative_log,
                details=details,
            )
        )
    return stage_result("coverage", gates, duration_seconds=time.monotonic() - started)
