"""Unit, integration, contract and end-to-end test stages."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

from ..context import Context
from ..errors import CiError
from ..gates import GateResult, StageResult, errored, failed, passed, skipped, stage_result
from ..runner import run_command
from ..util import iter_files, matches_any, parse_xml_file, relpath
from .base import Echo, disabled_gate

_DEFAULT_TEST_GLOBS = (
    "**/test_*.py",
    "**/*_test.py",
    "**/*.test.ts",
    "**/*.test.tsx",
    "**/*.test.js",
    "**/*.test.jsx",
    "**/*.test.mjs",
    "**/*.spec.ts",
    "**/*.spec.tsx",
    "**/*.spec.js",
    "**/*.spec.jsx",
    "**/*_test.go",
    "**/*.test.cs",
)
_ZERO_TEST_PATTERNS = (
    re.compile(r"(?i)\bno tests ran\b"),
    re.compile(r"(?i)\bcollected 0 items\b"),
    re.compile(r"(?i)\b0 tests? (?:run|executed|found)\b"),
    re.compile(r"(?i)\bTests:\s+0\b"),
    re.compile(r"(?i)\b0 passed\b"),
)
_TEST_DIRS = {
    ".git",
    ".ci",
    ".localci",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "dist",
    "build",
}


@dataclass(slots=True)
class JunitCounts:
    tests: int = 0
    failures: int = 0
    errors: int = 0
    skipped: int = 0
    suites: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "tests": self.tests,
            "failures": self.failures,
            "errors": self.errors,
            "skipped": self.skipped,
            "suites": self.suites,
        }


def discover_tests(root: Path, globs: list[str]) -> list[str]:
    patterns = tuple(globs or _DEFAULT_TEST_GLOBS)
    matches: list[str] = []
    for path in iter_files(root, excluded_dirs=_TEST_DIRS):
        relative = path.relative_to(root).as_posix()
        if matches_any(relative, patterns):
            matches.append(relative)
    return sorted(set(matches))


def parse_junit(path: Path) -> JunitCounts:
    counts = JunitCounts()
    root = parse_xml_file(path)
    candidates = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    if not candidates:
        candidates = [root]
    for suite in candidates:
        counts.suites += 1
        for attribute in ("tests", "failures", "errors", "skipped"):
            try:
                value = int(suite.attrib.get(attribute, "0"))
            except ValueError:
                value = 0
            setattr(counts, attribute, getattr(counts, attribute) + value)
    return counts


def _looks_like_zero_tests(output: str) -> bool:
    return any(pattern.search(output) for pattern in _ZERO_TEST_PATTERNS)


def run_test_stage(
    *,
    context: Context,
    name: str,
    echo: Echo,
    repeat: int = 1,
) -> StageResult:
    started = time.monotonic()
    stage = context.config.stage(name)
    if stage is None:
        return stage_result(
            name,
            [
                skipped(
                    f"{name}.config",
                    name,
                    f"{name} configured",
                    summary=f"[stages.{name}] is not configured",
                )
            ],
            duration_seconds=time.monotonic() - started,
            skipped_reason="not configured",
        )
    if not stage.enabled:
        return stage_result(
            name,
            [disabled_gate(stage=stage)],
            duration_seconds=time.monotonic() - started,
            skipped_reason="disabled",
        )
    if not stage.commands:
        if stage.required:
            gate = errored(
                f"{name}.config",
                name,
                f"{name} test command",
                required=True,
                summary="required test stage has no command",
                remediation=f"Add [stages.{name}] command or set required=false.",
            )
        else:
            gate = skipped(
                f"{name}.config",
                name,
                f"{name} test command",
                summary=f"[stages.{name}] has no command",
            )
        return stage_result(
            name,
            [gate],
            duration_seconds=time.monotonic() - started,
            skipped_reason=None if stage.required else "not configured",
        )

    gates: list[GateResult] = []
    discovered = discover_tests(context.root, stage.test_globs)
    minimum = stage.min_test_files
    if (
        minimum == 0
        and stage.required
        and context.config.policy.require_test_discovery
        and not stage.allow_empty
    ):
        minimum = 1
    if len(discovered) < minimum:
        gates.append(
            failed(
                f"{name}.discovery",
                name,
                "test discovery",
                required=stage.required,
                summary=(
                    f"found {len(discovered)} test file(s), minimum is {minimum}; "
                    "a configured test command must not silently run zero tests"
                ),
                details={"files": discovered[:50], "minimum": minimum},
                remediation=(
                    "Add test files, set test_globs, or set allow_empty=true only with a "
                    "documented reason."
                ),
            )
        )
    else:
        gates.append(
            passed(
                f"{name}.discovery",
                name,
                "test discovery",
                required=stage.required,
                summary=f"{len(discovered)} test file(s)",
                details={"files": discovered[:50]},
            )
        )

    if any(gate.blocking_failure for gate in gates):
        return stage_result(name, gates, duration_seconds=time.monotonic() - started)

    cwd = context.stage_cwd(stage)
    environment = context.stage_env(stage)
    for index, raw_command in enumerate(stage.commands, start=1):
        command = context.render(raw_command)
        run_ok = True
        run_failure_index: int | None = None
        passed_runs = 0
        tail = ""
        last_log = ""
        last_timed_out = False
        last_exit_code: int | None = None
        run_results: list[dict[str, object]] = []
        for run_number in range(1, max(1, repeat) + 1):
            log = context.paths.logs_dir / f"{name}-{index:02d}-run{run_number}.log"
            echo(f"  $ {command}" + (f"   (repeat {run_number}/{repeat})" if repeat > 1 else ""))
            result = run_command(
                command,
                cwd=cwd,
                env=environment,
                timeout=float(stage.timeout_seconds),
                log_path=log,
                echo=echo,
            )
            last_log = relpath(log, context.root)
            tail = result.output_tail
            run_results.append(
                {
                    "run": run_number,
                    "exit_code": result.exit_code,
                    "duration_seconds": round(result.duration_seconds, 3),
                    "timed_out": result.timed_out,
                    "log": last_log,
                }
            )
            last_timed_out = result.timed_out
            last_exit_code = result.exit_code
            if not result.ok:
                run_ok = False
                if run_failure_index is None:
                    run_failure_index = run_number
                break
            passed_runs += 1
            if repeat <= 1:
                break
        flaky = repeat > 1 and run_failure_index is not None and passed_runs > 0
        zero_tests = _looks_like_zero_tests(tail) and not stage.allow_empty
        if run_ok and zero_tests:
            run_ok = False
        summary = "passed"
        if run_failure_index is not None:
            summary = (
                f"flaky: run {run_failure_index} failed after {passed_runs} pass(es)"
                if flaky
                else f"failed on run {run_failure_index}"
            )
        elif zero_tests:
            summary = "command exited 0 but reported zero tests"
        if run_ok:
            gates.append(
                passed(
                    f"{name}.{index:02d}",
                    name,
                    command,
                    required=stage.required,
                    summary=summary + (f" ({repeat} runs)" if repeat > 1 else ""),
                    command=command,
                    exit_code=0,
                    log=last_log,
                    details={"runs": run_results, "flaky": False},
                )
            )
        else:
            exit_code = last_exit_code
            remediation: str | None = None
            if last_timed_out:
                remediation = (
                    "Increase stages.<name>.timeout only after confirming the command "
                    "terminates, or fix the hang."
                )
            elif exit_code == 127:
                remediation = (
                    "The command or one of its tools was not found. Install it or fix PATH/venv."
                )
            elif exit_code == 126:
                remediation = "The command is not executable. Check file permissions."
            # No verdict vs verdict (see stages/base.py): a missing tool,
            # a timeout, or a command that never started is recorded as the
            # absence of a measurement, never as a test failure.
            no_verdict = last_timed_out or exit_code is None or exit_code == 127
            factory = errored if no_verdict else failed
            details: dict[str, object] = {"runs": run_results, "flaky": flaky}
            if no_verdict:
                details["outcome"] = "unverified"
            gates.append(
                factory(
                    f"{name}.{index:02d}",
                    name,
                    command,
                    required=stage.required,
                    summary=summary,
                    command=command,
                    exit_code=exit_code,
                    log=last_log,
                    remediation=remediation,
                    details=details,
                )
            )
            break

    if stage.junit_path:
        junit_path = context.root / context.render(stage.junit_path)
        junit_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            relative_junit = relpath(junit_path, context.root)
        except ValueError:
            relative_junit = str(junit_path)
        if junit_path.is_file():
            try:
                counts = parse_junit(junit_path)
            except (CiError, OSError) as exc:
                gates.append(
                    failed(
                        f"{name}.junit",
                        name,
                        "JUnit results",
                        required=stage.required,
                        summary=f"could not parse {relative_junit}: {exc}",
                        remediation="Fix the JUnit reporter configuration.",
                    )
                )
            else:
                if stage.min_tests and counts.tests < stage.min_tests:
                    gates.append(
                        failed(
                            f"{name}.junit",
                            name,
                            "JUnit results",
                            required=stage.required,
                            summary=(
                                f"{counts.tests} test(s) reported, minimum is {stage.min_tests}"
                            ),
                            details=counts.to_dict(),
                        )
                    )
                else:
                    gates.append(
                        passed(
                            f"{name}.junit",
                            name,
                            "JUnit results",
                            required=stage.required,
                            summary=(
                                f"{counts.tests} tests, {counts.failures} failures, "
                                f"{counts.errors} errors, {counts.skipped} skipped"
                            ),
                            details=counts.to_dict(),
                            artifacts=[relative_junit],
                        )
                    )
        else:
            gates.append(
                failed(
                    f"{name}.junit",
                    name,
                    "JUnit results",
                    required=stage.required,
                    summary=f"configured JUnit file was not produced: {relative_junit}",
                    remediation=(
                        "Pass a JUnit output flag to the test command or remove "
                        f"[stages.{name}].junit_path."
                    ),
                )
            )

    return stage_result(name, gates, duration_seconds=time.monotonic() - started)
