"""Command-line interface for ci-fabric.

The CLI is a thin, explicit layer over the pipeline modules. Every run that
creates a :class:`~ci_fabric.context.Context` also writes a structured result
document, including command-level failures, so local evidence is never lost.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, NoReturn

from . import __version__
from .commands import (
    DEPLOY_COMMAND_HELP,
    PIPELINE_COMMAND_HELP,
)
from .config import PIPELINE_STAGES, PROFILES, STAGE_NAMES, Config, load_config
from .context import Context, create_context
from .deployment import (
    deploy,
    deploy_summary,
    health,
    rollback,
    smoke,
    status as deployment_status,
    undeploy,
)
from .docgen import REFERENCE_RELATIVE_PATH, generate_reference
from .doctor import diagnose, print_report
from .errors import (
    EXIT_CONFIG,
    EXIT_GATE_FAILED,
    EXIT_INTERRUPTED,
    EXIT_OK,
    CiError,
    UsageError,
)
from .gates import GateResult, StageResult, errored, passed, stage_result
from .hooks import hook_status, install_hooks, run_hook, uninstall_hooks
from .pipeline import ReleaseRequest, describe_pipeline, run_pipeline
from .release import list_releases, resolve_release
from .report import finalize, latest_result, report_paths
from .scaffold import scaffold
from .util import ensure_dir, isoformat, relpath, utc_now, write_json


class _RunLock:
    """Serialize state-mutating runs within one repository."""

    def __init__(self, root: Path) -> None:
        self.path = root / ".ci" / "run.lock"
        self._handle: Any = None

    def __enter__(self) -> None:
        ensure_dir(self.path.parent)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise CiError(
                "another ci-fabric run is already active in this repository",
                remediation=(
                    "Wait for it to finish. If it crashed, inspect .ci/ and remove "
                    "the stale .ci/run.lock only after confirming no process is running."
                ),
            ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()}\nstarted_at={isoformat(utc_now())}\n")
        handle.flush()
        self._handle = handle

    def __exit__(self, *args: object) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        with contextlib.suppress(OSError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _echo(message: str = "") -> None:
    print(message, flush=True)


def _fail(message: str, remediation: str | None = None, exit_code: int = EXIT_CONFIG) -> NoReturn:
    print(f"error: {message}", file=sys.stderr, flush=True)
    if remediation:
        print(f"fix:   {remediation}", file=sys.stderr, flush=True)
    raise SystemExit(exit_code)


def _find_root(start: Path) -> Path:
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / "ci.toml").is_file():
            return candidate
    return current


def _load(root: Path, config_path: str | None) -> Config:
    return load_config(root, Path(config_path) if config_path else None)


def _context(config: Config, *, clean_env: bool) -> Context:
    return create_context(config.root, config, clean_env=clean_env)


def _cli_stage(name: str, error: CiError) -> StageResult:
    gate = errored(
        f"{name}.error",
        name,
        f"{name} failed",
        required=True,
        summary=str(error),
        remediation=error.remediation,
    )
    return stage_result(name, [gate], error=str(error))


def _finalize_failure(
    context: Context,
    *,
    command: str,
    error: CiError,
    echo: Any = _echo,
    metadata: dict[str, Any] | None = None,
) -> int:
    stage = _cli_stage(command, error)
    exit_code = error.exit_code
    finalize(
        context,
        command=command,
        stages=[stage],
        status="FAIL",
        exit_code=exit_code,
        started_at=isoformat(context.started_at),
        release=None,
        error=str(error),
        metadata=metadata or {},
        echo=echo,
    )
    echo(f"  fix: {error.remediation}" if error.remediation else "")
    return exit_code


def _run_pipeline_command(args: argparse.Namespace, root: Path, config: Config) -> int:
    command: str = args.command
    selected: list[str] | None = None
    if getattr(args, "update_baseline", False):
        return _update_secrets_baseline(args, root, config)
    if command == "run":
        selected = []
        for value in args.stages or []:
            selected.extend(part.strip() for part in value.split(",") if part.strip())
        if not selected:
            selected = list(STAGE_NAMES)
        skip: set[str] = set()
        for value in args.skip or []:
            skip.update(part.strip() for part in value.split(",") if part.strip())
        unknown = sorted((set(selected) | skip) - set(PIPELINE_STAGES))
        if unknown:
            _fail(
                f"unknown stage(s): {', '.join(unknown)}",
                f"Known stages: {', '.join(PIPELINE_STAGES)}.",
            )
        selected = [name for name in selected if name not in skip]
        if not selected:
            _fail("no stages remain after --skip")
    request: ReleaseRequest | None = None
    if command == "release":
        request = ReleaseRequest(
            version=args.release_version,
            tag=args.tag,
            notes_file=args.notes_file,
            force=args.force,
        )
    with _RunLock(root):
        try:
            context = _context(config, clean_env=args.clean_env)
        except CiError as exc:
            _fail(str(exc), exc.remediation, exc.exit_code)
        echo = _echo
        try:
            result = run_pipeline(
                context,
                command=command,
                stages=selected,
                fix=getattr(args, "fix", False),
                release=request,
                echo=echo,
            )
        except CiError as exc:
            return _finalize_failure(context, command=command, error=exc)
        except KeyboardInterrupt:
            _print_interrupted()
            stage = stage_result(
                "interrupt",
                [
                    errored(
                        "run.interrupted",
                        "interrupt",
                        "run interrupted",
                        required=True,
                        summary="the run was interrupted by the user",
                        remediation="Re-run when ready; partial results are preserved.",
                    )
                ],
                error="interrupted",
            )
            finalize(
                context,
                command=command,
                stages=[stage],
                status="INTERRUPTED",
                exit_code=EXIT_INTERRUPTED,
                started_at=isoformat(context.started_at),
                release=None,
                error="interrupted",
                echo=echo,
            )
            return EXIT_INTERRUPTED
        finalize(
            context,
            command=result.command,
            stages=result.stages,
            status=result.status,
            exit_code=result.exit_code,
            started_at=result.started_at,
            release=result.release,
            error=result.error,
            metadata=result.metadata,
            echo=echo,
        )
        return result.exit_code


def _outcome_checks(outcome: Any) -> list[GateResult]:
    gates: list[GateResult] = []
    for check in outcome.checks:
        gate_id = f"{outcome.action}.{check.name}"
        if check.ok:
            gates.append(
                passed(
                    gate_id,
                    outcome.action,
                    check.name,
                    required=True,
                    summary=check.detail,
                    duration_seconds=check.duration_seconds,
                )
            )
        else:
            gates.append(
                errored(
                    gate_id,
                    outcome.action,
                    check.name,
                    required=True,
                    summary=check.detail,
                    duration_seconds=check.duration_seconds,
                )
            )
    if outcome.error and not any(gate.failed for gate in gates):
        gates.append(
            errored(
                f"{outcome.action}.error",
                outcome.action,
                outcome.action,
                required=True,
                summary=outcome.error,
                remediation=outcome.remediation,
            )
        )
    return gates


def _print_outcome(outcome: Any, *, echo: Any = _echo) -> None:
    for check in outcome.checks:
        marker = "OK  " if check.ok else "FAIL"
        echo(f"  {marker} {check.name}: {check.detail}")
    if outcome.record is not None:
        record = outcome.record
        echo(
            f"  deployment {record.deploy_id}: {record.version} "
            f"({record.strategy}) at {record.base_url} [{record.status}]"
        )
        if record.log:
            echo(f"  log: {record.log}")
    if outcome.rolled_back:
        echo(f"  automatic rollback restored {outcome.rollback_version}")
    if outcome.error:
        echo(f"  error: {outcome.error}")
    if outcome.remediation:
        echo(f"  fix: {outcome.remediation}")
    echo(f"{outcome.action.upper()} {'OK' if outcome.ok else 'FAILED'}")


def _run_deploy_command(args: argparse.Namespace, root: Path, config: Config) -> int:
    with _RunLock(root):
        try:
            context = _context(config, clean_env=args.clean_env)
        except CiError as exc:
            _fail(str(exc), exc.remediation, exc.exit_code)
        echo = _echo
        try:
            if args.command == "deploy":
                outcome = deploy(
                    context,
                    args.env,
                    version=args.deploy_version,
                    echo=echo,
                )
            elif args.command == "rollback":
                outcome = rollback(context, args.env, echo=echo)
            elif args.command == "undeploy":
                outcome = undeploy(context, args.env, echo=echo)
            elif args.command == "health":
                outcome = health(context, args.env)
            elif args.command == "smoke":
                outcome = smoke(context, args.env, echo=echo)
            else:  # pragma: no cover - parser keeps this unreachable
                raise UsageError(f"unsupported deployment command: {args.command}")
        except CiError as exc:
            return _finalize_failure(
                context,
                command=args.command,
                error=exc,
                metadata={"environment": getattr(args, "env", None)},
            )
        except KeyboardInterrupt:
            _print_interrupted()
            return EXIT_INTERRUPTED
        _print_outcome(outcome, echo=echo)
        stage = stage_result(
            outcome.action,
            _outcome_checks(outcome),
            error=None if outcome.ok else outcome.error,
        )
        payload = outcome.to_dict()
        exit_code = EXIT_OK if outcome.ok else EXIT_GATE_FAILED
        finalize(
            context,
            command=args.command,
            stages=[stage],
            status="PASS" if outcome.ok else "FAIL",
            exit_code=exit_code,
            started_at=isoformat(context.started_at),
            release=None,
            error=None if outcome.ok else outcome.error,
            metadata={
                "environment": outcome.environment,
                "deployment": payload,
            },
            echo=echo,
        )
        return exit_code


def _print_interrupted() -> None:
    print("interrupted", file=sys.stderr, flush=True)


def _cmd_init(args: argparse.Namespace) -> int:
    root = (Path(args.root).expanduser() if args.root else Path.cwd()).resolve()
    if not root.is_dir():
        _fail(f"target directory does not exist: {root}")
    try:
        result = scaffold(root, profile=args.profile, force=args.force)
    except CiError as exc:
        _fail(str(exc), exc.remediation, exc.exit_code)
    detection = result.detection
    _echo(f"initialized {relpath(result.config_path, root)}")
    _echo(f"  project: {detection.name} {detection.version or '(unversioned)'}")
    _echo(f"  stacks:  {', '.join(detection.language_names) or 'none detected'}")
    for note in dict.fromkeys(detection.notes):
        _echo(f"  note:    {note}")
    if result.gitignore_updated:
        _echo("  updated .gitignore with .ci/ and .localci/")
    if result.shim_written:
        _echo("  created executable ./ci")
    else:
        _echo("  ./ci already present")
    try:
        config = load_config(root)
    except CiError as exc:
        _fail(
            f"generated configuration did not load: {exc}",
            "Review ci.toml and re-run init with --force after fixing the cause.",
        )
    _echo(f"  stages:  {', '.join(name for name in STAGE_NAMES if config.stage(name))}")
    _echo("review ci.toml before running ./ci ci")
    return EXIT_OK


def _json_echo(message: str) -> None:
    """Human progress lines must never pollute `--json` stdout."""
    print(message, file=sys.stderr, flush=True)


def _cmd_doctor(args: argparse.Namespace, root: Path, config: Config) -> int:
    context = create_context(config.root, config, clean_env=args.clean_env)
    report = diagnose(context)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        print_report(report)
    gates: list[GateResult] = []
    for check in report.checks:
        factory = passed if check.ok else errored
        gates.append(
            factory(
                f"doctor.{check.name}",
                "doctor",
                check.name,
                required=check.required,
                summary=check.detail,
                remediation=check.remediation,
            )
        )
    stage = stage_result("doctor", gates)
    exit_code = EXIT_OK if report.ok else EXIT_GATE_FAILED
    finalize(
        context,
        command="doctor",
        stages=[stage],
        status="PASS" if report.ok else "FAIL",
        exit_code=exit_code,
        started_at=isoformat(context.started_at),
        release=None,
        error=None if report.ok else "required diagnostics failed",
        metadata={"doctor": report.to_dict()},
        echo=_json_echo if args.json else _echo,
    )
    return exit_code


def _cmd_policy(args: argparse.Namespace, root: Path, config: Config) -> int:
    document: dict[str, Any] = {
        "schema_version": 1,
        "tool": {"name": "ci-fabric", "version": __version__},
        "config": relpath(config.source_path, root),
        "policy": config.policy_document(),
        "pipeline": describe_pipeline(config),
    }
    if args.json:
        print(json.dumps(document, indent=2, sort_keys=True))
        return EXIT_OK
    policy = document["policy"]
    print(f"profile: {policy['profile']}")
    print(f"fail_fast: {policy['fail_fast']}")
    print(f"require_clean_worktree: {policy['require_clean_worktree']}")
    print(f"keep_runs: {policy['keep_runs']}")
    print(f"required_stages: {', '.join(policy['required_stages'])}")
    print("")
    print(f"{'stage':<14} {'configured':<11} {'required':<9} classification")
    for stage in document["pipeline"]:
        print(
            f"{stage['stage']:<14} {stage['configured']!s:<11} "
            f"{stage['required']!s:<9} {stage['classification']}"
        )
    print("")
    print("Use `./ci policy --json` for the machine-readable document.")
    return EXIT_OK


def _cmd_status(args: argparse.Namespace, root: Path, config: Config) -> int:
    context = create_context(config.root, config, clean_env=args.clean_env)
    latest = latest_result(context)
    hooks = hook_status(root)
    deployments = deploy_summary(context)
    document: dict[str, Any] = {
        "project": {
            "name": config.project.name,
            "version": config.project.version,
            "profile": config.policy.profile,
        },
        "git": context.git.to_dict(),
        "last_run": latest,
        "hooks": hooks.to_dict(),
        "deployments": deployments,
        "paths": report_paths(context),
    }
    if args.json:
        print(json.dumps(document, indent=2, sort_keys=True, default=str))
    else:
        print(f"{config.project.name} {config.project.version or '(unversioned)'}")
        print(
            f"  profile: {config.policy.profile}; "
            f"git: {context.git.short_revision or 'nogit'} "
            f"({context.git.branch or 'no branch'}, "
            f"{'dirty' if context.git.dirty else 'clean'})"
        )
        if latest is None:
            print("  last run: none")
        else:
            print(
                f"  last run: {latest.get('run', {}).get('id')} "
                f"{latest.get('status')} (exit {latest.get('exit_code')}) "
                f"{latest.get('command')}"
            )
        print(
            f"  hooks: {'installed' if hooks.installed else 'not installed'}"
            + (f" ({', '.join(hooks.hooks)})" if hooks.hooks else "")
        )
        if deployments:
            print("  deployments:")
            for item in deployments:
                if item.get("error"):
                    print(f"    - {item['environment']}: error: {item['error']}")
                    continue
                state = "running" if item.get("running") else "stopped"
                print(
                    f"    - {item['environment']}: "
                    f"{item.get('version') or 'none'} ({state}) "
                    f"{item.get('base_url') or ''}"
                )
        else:
            print("  deployments: none configured")
    stage = stage_result(
        "status",
        [
            passed(
                "status.inspected",
                "status",
                "repository status",
                required=False,
                summary=(
                    f"{len(deployments)} deployment environment(s); "
                    f"hooks {'installed' if hooks.installed else 'not installed'}"
                ),
            )
        ],
    )
    finalize(
        context,
        command="status",
        stages=[stage],
        status="PASS",
        exit_code=EXIT_OK,
        started_at=isoformat(context.started_at),
        release=None,
        error=None,
        metadata={"hooks": hooks.to_dict(), "deployments": deployments},
        echo=_json_echo if args.json else _echo,
    )
    return EXIT_OK


def _cmd_deploy_status(args: argparse.Namespace, root: Path, config: Config) -> int:
    context = create_context(config.root, config, clean_env=args.clean_env)
    if args.env:
        info = deployment_status(context, args.env, check_http=not args.no_http)
    else:
        info = {"environments": deploy_summary(context)}
    if args.json:
        print(json.dumps(info, indent=2, sort_keys=True, default=str))
    else:
        if "environments" in info:
            for item in info["environments"]:
                print(
                    f"{item['environment']}: {item.get('version') or 'none'} "
                    f"({'running' if item.get('running') else 'stopped'}) "
                    f"{item.get('base_url') or ''}"
                )
        else:
            record = info.get("record") or {}
            print(
                f"{info.get('environment')}: "
                f"{record.get('version', 'none')} "
                f"({'running' if info.get('running') else 'stopped'}) "
                f"{record.get('base_url', '')}"
            )
            if info.get("health"):
                health_info = info["health"]
                print(
                    f"  health: {'ok' if health_info.get('ok') else 'fail'} "
                    f"{health_info.get('detail', '')}"
                )
    ok = bool(info.get("running")) if info.get("deployed") else True
    stage = stage_result(
        "deployment-status",
        [
            passed(
                "deployment-status.inspected",
                "deployment-status",
                "deployment status",
                required=False,
                summary=f"environment: {args.env or 'all'}",
            )
        ],
    )
    finalize(
        context,
        command="deploy-status",
        stages=[stage],
        status="PASS" if ok else "WARN",
        exit_code=EXIT_OK,
        started_at=isoformat(context.started_at),
        release=None,
        error=None,
        metadata={"deployments": info},
        echo=_json_echo if args.json else _echo,
    )
    return EXIT_OK


def _cmd_clean(args: argparse.Namespace, root: Path, config: Config) -> int:
    keep = args.keep if args.keep is not None else config.policy.keep_runs
    if keep < 1:
        _fail("--keep must be at least 1")
    context = create_context(config.root, config, clean_env=args.clean_env)
    removed: list[str] = []
    results_dir = context.paths.results_dir
    for path in sorted(results_dir.glob("*.json")):
        if path.name == "latest.json" or path == context.paths.result_path:
            continue
        run_id = path.stem
        removed.append(f"results/{run_id}.json")
        with contextlib.suppress(OSError):
            path.unlink()
        for collection in ("logs", "reports", "artifacts", "tmp"):
            directory = context.paths.state_dir / collection / run_id
            if directory.is_dir():
                removed.append(f"{collection}/{run_id}")
                with contextlib.suppress(OSError):
                    shutil.rmtree(directory, ignore_errors=True)
    if args.all:
        for collection in ("logs", "reports", "artifacts", "tmp"):
            base = context.paths.state_dir / collection
            if not base.is_dir():
                continue
            for directory in base.iterdir():
                if directory.is_dir() and directory.name != context.run_id:
                    removed.append(f"{collection}/{directory.name}")
                    with contextlib.suppress(OSError):
                        shutil.rmtree(directory, ignore_errors=True)
    # Keep the newest configured number of result documents.
    entries = sorted(
        (path for path in results_dir.glob("*.json") if path.name != "latest.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in entries[max(1, keep) :]:
        removed.append(f"results/{path.stem}.json")
        with contextlib.suppress(OSError):
            path.unlink()
    stage = stage_result(
        "clean",
        [
            passed(
                "clean.state",
                "clean",
                "run state cleanup",
                required=False,
                summary=(
                    f"removed {len(removed)} path(s); kept {min(len(entries), max(1, keep))} run(s)"
                ),
            )
        ],
    )
    print(f"removed {len(removed)} path(s)")
    for item in removed[:100]:
        print(f"  - {item}")
    finalize(
        context,
        command="clean",
        stages=[stage],
        status="PASS",
        exit_code=EXIT_OK,
        started_at=isoformat(context.started_at),
        release=None,
        error=None,
        metadata={"removed": removed, "keep": keep},
        echo=_echo,
    )
    return EXIT_OK


def _cmd_hooks(args: argparse.Namespace, root: Path, config: Config) -> int:
    del config
    if args.hook_command == "install":
        if not (root / "ci").is_file():
            _fail(
                "the canonical ./ci entry point is missing",
                "Run `./ci init` (or `ci-fabric init`) before installing hooks.",
            )
        state = install_hooks(root, force=args.force)
        print(f"hooks installed: {state.hooks_path}")
        if state.previous_hooks_path:
            print(f"  chaining previous hooks: {state.previous_hooks_path}")
        if state.hooks:
            print(f"  chained: {', '.join(state.hooks)}")
        return EXIT_OK
    if args.hook_command == "uninstall":
        state = uninstall_hooks(root, force=args.force)
        print("hooks uninstalled")
        if state.previous_hooks_path:
            print(f"  restored core.hooksPath: {state.previous_hooks_path}")
        return EXIT_OK
    state = hook_status(root)
    print(f"installed: {'true' if state.installed else 'false'}")
    if state.hooks_path:
        print(f"hooksPath: {state.hooks_path}")
    if state.previous_hooks_path:
        print(f"previous:  {state.previous_hooks_path}")
    if state.hooks:
        print(f"hooks:     {', '.join(state.hooks)}")
    return EXIT_OK if state.installed else EXIT_GATE_FAILED


def _cmd_version(args: argparse.Namespace, root: Path, config: Config) -> int:
    del args, root, config
    print(f"ci-fabric {__version__}")
    return EXIT_OK


def _cmd_docs(args: argparse.Namespace, root: Path, config: Config) -> int:
    context = create_context(config.root, config, clean_env=args.clean_env)
    text = generate_reference(config, context)
    target = root / REFERENCE_RELATIVE_PATH
    current = target.read_text(encoding="utf-8") if target.is_file() else None
    changed = current != text

    if args.check:
        gate = (
            passed(
                "docs.reference", "docs", "generated reference", required=True, summary="up to date"
            )
            if not changed
            else errored(
                "docs.reference",
                "docs",
                "generated reference",
                required=True,
                summary=f"{REFERENCE_RELATIVE_PATH} is stale",
                remediation="Run `./ci docs`, review the diff, and commit it.",
            )
        )
    else:
        if changed:
            target.parent.mkdir(parents=True, exist_ok=True)
            from .util import write_text_atomic

            write_text_atomic(target, text)
        gate = passed(
            "docs.reference",
            "docs",
            "generated reference",
            required=True,
            summary=f"{REFERENCE_RELATIVE_PATH} {'regenerated' if changed else 'unchanged'}",
            artifacts=[REFERENCE_RELATIVE_PATH] if changed else [],
        )

    _echo(f"{REFERENCE_RELATIVE_PATH}: {'changed' if changed else 'unchanged'}")
    stage = stage_result("docs", [gate])
    exit_code = EXIT_OK if (gate.status == "PASS" or not args.check) else EXIT_GATE_FAILED
    finalize(
        context,
        command="docs",
        stages=[stage],
        status="PASS" if not (args.check and changed) else "FAIL",
        exit_code=exit_code,
        started_at=isoformat(context.started_at),
        release=None,
        error=None,
        metadata={"check": bool(args.check), "changed": changed},
        echo=_echo,
    )
    return exit_code


def _update_secrets_baseline(args: argparse.Namespace, root: Path, config: Config) -> int:
    """Record the current secret-scan findings as accepted legacy debt.

    The baseline is written from the same scanner the gate uses, so accepting
    today's findings can never accept tomorrow's. The command refuses to run
    without a configured `[stages.security].baseline` — a baseline written
    nowhere in particular is a finding file, not a gate input.
    """
    from .security import scan_repository

    with _RunLock(root):
        try:
            context = _context(config, clean_env=args.clean_env)
        except CiError as exc:
            _fail(str(exc), exc.remediation, exc.exit_code)
        stage = config.stage("security")
        target_rel = stage.baseline if stage else None
        if not target_rel:
            _fail(
                "no [stages.security].baseline is configured",
                'Add `baseline = "secrets-baseline.json"` to [stages.security], '
                "then re-run `./ci security --update-baseline` and commit the file.",
            )
        report = scan_repository(
            context.root,
            include_untracked=stage.scan_untracked if stage else False,
            allowlist=stage.allowlist if stage else [],
            exclude_globs=stage.exclude if stage else [],
        )
        document = {
            "schema_version": 1,
            "generated_by": "ci-fabric security --update-baseline",
            "generated_at": isoformat(context.started_at),
            "note": (
                "Accepted findings are visible debt, not permission: new findings "
                "still fail the gate. Re-run --update-baseline after fixing an "
                "accepted finding to shrink this list."
            ),
            "accepted": [finding.to_dict() for finding in report.findings],
        }
        target = (root / context.render(target_rel)).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        write_json(target, document)
        _echo(f"accepted {len(report.findings)} finding(s) -> {relpath(target, root)}")
        _echo("commit the baseline so the gate can read it on every run")
        gate = passed(
            "security.baseline",
            "security",
            "secrets baseline",
            required=False,
            summary=f"{len(report.findings)} finding(s) accepted as legacy debt",
            artifacts=[relpath(target, root)],
        )
        finalize(
            context,
            command="security",
            stages=[stage_result("security", [gate])],
            status="PASS",
            exit_code=EXIT_OK,
            started_at=isoformat(context.started_at),
            release=None,
            error=None,
            metadata={"baseline": relpath(target, root), "accepted": len(report.findings)},
            echo=_echo,
        )
        return EXIT_OK


def _cmd_releases(args: argparse.Namespace, root: Path, config: Config) -> int:
    versions = list_releases(root, config)
    if args.json:
        payload = []
        for version in versions:
            try:
                bundle = resolve_release(root, config, version)
            except CiError as exc:
                payload.append({"version": version, "error": str(exc)})
            else:
                payload.append({"version": version, **bundle.metadata})
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return EXIT_OK
    if not versions:
        print("no local releases")
        return EXIT_OK
    for version in versions:
        marker = ""
        try:
            bundle = resolve_release(root, config, version)
        except CiError as exc:
            marker = f" (invalid: {exc})"
        else:
            marker = f" {bundle.metadata.get('short_revision') or ''}"
        print(f"{version}{marker}")
    return EXIT_OK


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ci",
        description=(
            "Local-first CI/CD orchestration. All state stays under .ci/ and "
            ".localci/; no hosted service is contacted."
        ),
    )
    parser.add_argument("--root", help="repository root (default: discover ci.toml upward)")
    parser.add_argument("--config", help="alternate ci.toml path")
    parser.add_argument(
        "--clean-env",
        action="store_true",
        help="run with a minimal environment instead of inheriting the shell",
    )
    parser.add_argument(
        "--version", action="version", version=f"ci-fabric {__version__}", help="show version"
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    init = sub.add_parser("init", help="generate ci.toml and the ./ci entry point")
    init.add_argument("--profile", choices=PROFILES, default="standard")
    init.add_argument("--force", action="store_true", help="overwrite an existing ci.toml")
    init.set_defaults(handler=_cmd_init)

    doctor = sub.add_parser("doctor", help="diagnose environment and tooling problems")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(handler=_cmd_doctor)

    for name, help_text in PIPELINE_COMMAND_HELP:
        command = sub.add_parser(name, help=help_text)
        if name == "format":
            command.add_argument("--fix", action="store_true", help="apply the fix command")
        if name == "security":
            command.add_argument(
                "--update-baseline",
                action="store_true",
                help="record current findings as accepted in the configured baseline, then exit",
            )
        command.set_defaults(handler="pipeline")

    run = sub.add_parser("run", help="run an explicit stage selection")
    run.add_argument(
        "--stages",
        action="append",
        metavar="STAGE[,STAGE]",
        help="stages to run (default: all); repeatable",
    )
    run.add_argument(
        "--skip",
        action="append",
        metavar="STAGE[,STAGE]",
        help="stages to skip; repeatable",
    )
    run.set_defaults(handler="pipeline")

    release = sub.add_parser("release", help="run the full pipeline and create a local release")
    release.add_argument("--version", dest="release_version", help="release version override")
    release.add_argument("--tag", action="store_true", help="create an annotated Git tag")
    release.add_argument("--notes-file", help="release notes file")
    release.add_argument("--force", action="store_true", help="replace an existing local release")
    release.set_defaults(handler="pipeline")

    deploy_parser = sub.add_parser("deploy", help="deploy a verified local release")
    deploy_parser.add_argument("--env", required=True, help="deployment environment name")
    deploy_parser.add_argument("--version", dest="deploy_version", help="release version")
    deploy_parser.set_defaults(handler="deploy")

    deploy_status = sub.add_parser("deploy-status", help="show deployment state")
    deploy_status.add_argument("--env", help="one environment (default: all)")
    deploy_status.add_argument("--json", action="store_true")
    deploy_status.add_argument("--no-http", action="store_true", help="skip HTTP probing")
    deploy_status.set_defaults(handler=_cmd_deploy_status)

    for name, help_text in DEPLOY_COMMAND_HELP:
        if name in {"deploy", "deploy-status"}:
            continue  # defined above with their specific options and handlers
        command = sub.add_parser(name, help=help_text)
        command.add_argument("--env", required=True, help="deployment environment name")
        command.set_defaults(handler="deploy")

    hooks = sub.add_parser("hooks", help="install, remove or inspect Git hooks")
    hook_sub = hooks.add_subparsers(dest="hook_command", required=True)
    hook_install = hook_sub.add_parser("install", help="install chaining hooks")
    hook_install.add_argument("--force", action="store_true")
    hook_install.set_defaults(handler=_cmd_hooks)
    hook_uninstall = hook_sub.add_parser("uninstall", help="remove hooks and restore prior config")
    hook_uninstall.add_argument("--force", action="store_true")
    hook_uninstall.set_defaults(handler=_cmd_hooks)
    hook_status_parser = hook_sub.add_parser("status", help="show hook state")
    hook_status_parser.set_defaults(handler=_cmd_hooks)

    policy = sub.add_parser("policy", help="show the effective policy")
    policy.add_argument("--json", action="store_true")
    policy.set_defaults(handler=_cmd_policy)

    status = sub.add_parser("status", help="show repository, run, hook and deployment status")
    status.add_argument("--json", action="store_true")
    status.set_defaults(handler=_cmd_status)

    releases = sub.add_parser("releases", help="list verified local releases")
    releases.add_argument("--json", action="store_true")
    releases.set_defaults(handler=_cmd_releases)

    clean = sub.add_parser("clean", help="remove old run state")
    clean.add_argument("--all", action="store_true", help="remove every previous run")
    clean.add_argument("--keep", type=int, help="number of recent runs to keep")
    clean.set_defaults(handler=_cmd_clean)

    docs = sub.add_parser("docs", help="regenerate docs/reference.md from the live code")
    docs.add_argument("--check", action="store_true", help="fail if stale; do not write")
    docs.set_defaults(handler=_cmd_docs)

    version = sub.add_parser("version", help="print the tool version")
    version.set_defaults(handler=_cmd_version)

    hook_run = sub.add_parser("hook-run", help=argparse.SUPPRESS)
    hook_run.add_argument("hook")
    hook_run.add_argument("args", nargs="*")
    hook_run.set_defaults(handler="hook-run")

    return parser


def _dispatch(args: argparse.Namespace) -> int:
    command = args.command
    clean_env = bool(getattr(args, "clean_env", False))
    if command == "init":
        return _cmd_init(args)
    start = Path(args.root).expanduser() if args.root else Path.cwd()
    root = _find_root(start)
    config = _load(root, args.config)
    handler = args.handler
    if handler == "pipeline":
        return _run_pipeline_command(args, root, config)
    if handler == "deploy":
        return _run_deploy_command(args, root, config)
    if command == "hook-run":
        if args.hook not in {"pre-commit", "pre-push"}:
            _fail(f"unsupported hook: {args.hook}")
        with _RunLock(root):
            context = _context(config, clean_env=clean_env)
            return run_hook(context, args.hook, echo=_echo)
    return int(handler(args, root, config))


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return _dispatch(args)
    except CiError as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        if exc.remediation:
            print(f"fix:   {exc.remediation}", file=sys.stderr, flush=True)
        return exc.exit_code
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr, flush=True)
        return EXIT_INTERRUPTED
    except BrokenPipeError:
        return EXIT_OK
    except SystemExit:
        raise
    except Exception as exc:
        print(f"internal error: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return EXIT_GATE_FAILED


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
