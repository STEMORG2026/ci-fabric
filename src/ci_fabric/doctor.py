"""Environment and tooling diagnostics before running the pipeline."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import command_program
from .context import Context
from .errors import CiError
from .util import git_is_ignored, port_is_open, utc_now

_SHELL_BUILTINS = {
    "test",
    "[",
    "]",
    "true",
    "false",
    "cd",
    "echo",
    "printf",
    "export",
    "set",
    "unset",
    "if",
    "for",
    "while",
    "case",
    "shift",
    "trap",
    "exit",
    "command",
    "read",
}


@dataclass(slots=True)
class DoctorCheck:
    name: str
    ok: bool
    required: bool
    detail: str
    remediation: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "required": self.required,
            "detail": self.detail,
            "remediation": self.remediation,
        }


@dataclass(slots=True)
class DoctorReport:
    checks: list[DoctorCheck] = field(default_factory=list)
    started_at: str = ""

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks if check.required)

    @property
    def failures(self) -> list[DoctorCheck]:
        return [check for check in self.checks if not check.ok and check.required]

    @property
    def warnings(self) -> list[DoctorCheck]:
        return [check for check in self.checks if not check.ok and not check.required]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "started_at": self.started_at,
            "checks": [check.to_dict() for check in self.checks],
            "failures": [check.name for check in self.failures],
            "warnings": [check.name for check in self.warnings],
        }


def _module_available(interpreter: Path, module: str) -> bool:
    script = (
        f"import importlib.util, sys; sys.exit(0 if importlib.util.find_spec({module!r}) else 1)"
    )
    try:
        result = subprocess.run(
            [str(interpreter), "-c", script],
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _python_module(command: str) -> str | None:
    import shlex

    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    for index, word in enumerate(tokens[:-1]):
        if word == "-m":
            return tokens[index + 1]
    return None


def _command_checks(context: Context) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    path = context.base_env.get("PATH")
    module_cache: dict[tuple[Path, str], bool] = {}
    node_dependency_seen: set[Path] = set()
    for stage_name, stage in sorted(context.config.stages.items()):
        if not stage.enabled:
            continue
        for raw_command in stage.commands:
            # Commands are checked exactly as they will run: placeholders such
            # as {python} must be rendered first, or every templated command
            # would be reported as a missing executable.
            try:
                command = context.render(raw_command)
            except CiError as exc:
                checks.append(
                    DoctorCheck(
                        f"command.{stage_name}.render",
                        False,
                        stage.required,
                        f"could not render command `{raw_command}`: {exc}",
                        "Fix the unknown placeholder in the stage command.",
                    )
                )
                continue
            program = command_program(command)
            if not program or program in _SHELL_BUILTINS:
                continue
            if any(character in program for character in "$`();<>|&"):
                continue
            resolved = shutil.which(program, path=path)
            required = stage.required
            if resolved is None:
                checks.append(
                    DoctorCheck(
                        f"command.{stage_name}.{program}",
                        False,
                        required,
                        f"`{program}` is not on PATH (stage {stage_name})",
                        (
                            "Install the tool, activate the project virtual environment, "
                            "or run scripts/bootstrap.sh."
                        ),
                    )
                )
                continue
            module = _python_module(command)
            if module and program.startswith("python"):
                interpreter = Path(resolved)
                key = (interpreter, module)
                if key not in module_cache:
                    module_cache[key] = _module_available(interpreter, module)
                if not module_cache[key]:
                    checks.append(
                        DoctorCheck(
                            f"module.{stage_name}.{module}",
                            False,
                            required,
                            f"Python module `{module}` is not importable by {interpreter}",
                            "Install the development toolchain: "
                            f"{interpreter} -m pip install -r requirements-dev.lock",
                        )
                    )
                    continue
            checks.append(
                DoctorCheck(
                    f"command.{stage_name}.{program}",
                    True,
                    required,
                    f"{program} -> {resolved}",
                )
            )
            if program in {"npm", "pnpm", "yarn", "bun"}:
                node_modules = context.root / "node_modules"
                if not node_modules.is_dir() and node_modules not in node_dependency_seen:
                    node_dependency_seen.add(node_modules)
                    checks.append(
                        DoctorCheck(
                            "node.dependencies",
                            False,
                            False,
                            "node_modules is missing; JS stages may not start",
                            "Run the package manager install command (for example `npm ci` "
                            "or `pnpm install --frozen-lockfile`).",
                        )
                    )
    return checks


def diagnose(context: Context) -> DoctorReport:
    started = utc_now()
    checks: list[DoctorCheck] = []
    checks.append(
        DoctorCheck(
            "python.runtime",
            sys.version_info >= (3, 11),
            True,
            f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "ci-fabric requires Python 3.11+.",
        )
    )
    shell = shutil.which("sh", path=context.base_env.get("PATH")) or shutil.which("bash")
    checks.append(
        DoctorCheck(
            "shell.posix",
            bool(shell),
            True,
            shell or "no POSIX shell found",
            "Install /bin/sh; command execution is POSIX-only.",
        )
    )
    checks.append(
        DoctorCheck(
            "git.repository",
            context.git.available,
            not context.config.policy.allow_no_git,
            (
                f"{context.git.branch or 'detached'} @ {context.git.short_revision or 'no commits'}"
                if context.git.available
                else "not a Git repository"
            ),
            "Initialize Git; release, change attribution, and clean-tree gates require it.",
        )
    )
    checks.append(
        DoctorCheck(
            "git.worktree",
            not context.git.dirty or context.config.policy.require_clean_worktree != "always",
            False,
            f"{len(context.git.dirty_files)} changed path(s)" if context.git.dirty else "clean",
            "Commit or stash changes before release.",
        )
    )
    if context.git.available:
        ignored = all(git_is_ignored(context.root, path) for path in (".ci/", ".localci/"))
        checks.append(
            DoctorCheck(
                "git.state-ignored",
                ignored,
                False,
                "CI state is ignored" if ignored else ".ci/ or .localci/ is not ignored",
                "Run `./ci init` or add both paths to .gitignore.",
            )
        )
    venv = context.root / ".venv"
    checks.append(
        DoctorCheck(
            "python.venv",
            venv.is_dir(),
            False,
            str(venv) if venv.is_dir() else "no .venv in the repository",
            "Create one with `python3 -m venv .venv` and install the project's tools.",
        )
    )
    checks.extend(
        DoctorCheck(
            f"lockfile.{relative}",
            (context.root / relative).is_file(),
            True,
            relative,
            "Commit the lockfile; dependency resolution must be reproducible.",
        )
        for relative in context.config.repository.required_lockfiles
    )
    checks.extend(_command_checks(context))
    for name, environment in sorted(context.config.deploy.items()):
        missing = [item for item in environment.env_required if item not in os.environ]
        checks.append(
            DoctorCheck(
                f"deploy.{name}.environment",
                not missing,
                True,
                ("required variables present" if not missing else "missing: " + ", ".join(missing)),
                (
                    "Export the variables in the deployment shell. Values are never read "
                    "from ci.toml."
                ),
            )
        )
        if environment.port:
            port_free = not port_is_open(environment.host, environment.port)
            checks.append(
                DoctorCheck(
                    f"deploy.{name}.port",
                    port_free,
                    False,
                    (
                        f"{environment.host}:{environment.port} is free"
                        if port_free
                        else f"{environment.host}:{environment.port} is already in use"
                    ),
                    "Stop the existing service or set port = 0 for automatic allocation.",
                )
            )
    if context.config.release.signer == "cosign-keyless":
        cosign_path = shutil.which("cosign", path=context.base_env.get("PATH"))
        checks.append(
            DoctorCheck(
                "release.cosign",
                cosign_path is not None,
                True,
                (f"cosign -> {cosign_path}" if cosign_path else "cosign not on PATH"),
                'Install cosign, or set [release].signer = "none".',
            )
        )
        missing_fields = [
            name
            for name, value in (
                (
                    "signing_identity",
                    context.config.release.signing_identity
                    or os.environ.get("COSIGN_CERTIFICATE_IDENTITY"),
                ),
                (
                    "signing_issuer",
                    context.config.release.signing_issuer
                    or os.environ.get("COSIGN_CERTIFICATE_OIDC_ISSUER"),
                ),
            )
            if not value
        ]
        checks.append(
            DoctorCheck(
                "release.cosign-identity",
                not missing_fields,
                True,
                (
                    "signing identity and issuer configured"
                    if not missing_fields
                    else "missing: " + ", ".join(missing_fields)
                ),
                "Set [release].signing_identity and [release].signing_issuer, or "
                "export COSIGN_CERTIFICATE_IDENTITY / COSIGN_CERTIFICATE_OIDC_ISSUER.",
            )
        )
    try:
        free = shutil.disk_usage(context.root).free
        checks.append(
            DoctorCheck(
                "disk.free",
                free >= 1024**3,
                False,
                f"{free / (1024**3):.1f} GiB free",
                "Free disk space before build, package, or deploy.",
            )
        )
    except OSError as exc:
        checks.append(DoctorCheck("disk.free", False, False, f"could not inspect disk: {exc}"))
    return DoctorReport(checks=checks, started_at=started.replace(microsecond=0).isoformat())


def print_report(report: DoctorReport, *, echo: Any = print) -> None:
    for check in report.checks:
        marker = "OK  " if check.ok else ("FAIL" if check.required else "WARN")
        echo(f"{marker} {check.name}: {check.detail}")
        if not check.ok and check.remediation:
            echo(f"     fix: {check.remediation}")
    echo("")
    echo(
        f"doctor: {'OK' if report.ok else 'BLOCKED'} "
        f"({len(report.failures)} required failure(s), {len(report.warnings)} warning(s))"
    )
