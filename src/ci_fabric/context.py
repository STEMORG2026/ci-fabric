"""Run context: identity, paths, Git state, environment and placeholders."""

from __future__ import annotations

import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Config, StageConfig
from .errors import CiError, ConfigError, PrerequisiteError
from .util import (
    ensure_dir,
    git_output,
    git_root,
    isoformat,
    read_text,
    relpath,
    run_git,
    utc_now,
)

_PLACEHOLDER = re.compile(r"(?<!\$)\{([a-z][a-z0-9_]*)\}")
_VERSION = re.compile(r"^\d+\.\d+\.\d+")

_TOOL_CANDIDATES = (
    "python3",
    "git",
    "node",
    "npm",
    "pnpm",
    "yarn",
    "bun",
    "uv",
    "ruff",
    "pytest",
    "mypy",
    "coverage",
    "bandit",
    "pip-audit",
    "pip_audit",
    "docker",
    "podman",
    "dotnet",
    "go",
    "cargo",
    "rustc",
    "java",
    "shellcheck",
    "syft",
    "cosign",
)


@dataclass(slots=True)
class GitState:
    available: bool
    root: Path | None
    branch: str
    revision: str
    short_revision: str
    dirty: bool
    dirty_files: list[str]
    commit_epoch: int | None
    tags_at_head: list[str]
    tracked_files: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "root": str(self.root) if self.root else None,
            "branch": self.branch,
            "revision": self.revision,
            "short_revision": self.short_revision,
            "dirty": self.dirty,
            "dirty_files": self.dirty_files,
            "commit_epoch": self.commit_epoch,
            "tags_at_head": self.tags_at_head,
            "tracked_files": self.tracked_files,
        }


@dataclass(slots=True)
class RunPaths:
    state_dir: Path
    logs_dir: Path
    reports_dir: Path
    artifacts_dir: Path
    results_dir: Path
    result_path: Path
    latest_path: Path
    tmp_dir: Path

    @classmethod
    def create(cls, root: Path, run_id: str) -> RunPaths:
        state = root / ".ci"
        results = state / "results"
        paths = cls(
            state_dir=state,
            logs_dir=state / "logs" / run_id,
            reports_dir=state / "reports" / run_id,
            artifacts_dir=state / "artifacts" / run_id,
            results_dir=results,
            result_path=results / f"{run_id}.json",
            latest_path=results / "latest.json",
            tmp_dir=state / "tmp" / run_id,
        )
        for directory in (
            paths.logs_dir,
            paths.reports_dir,
            paths.artifacts_dir,
            paths.results_dir,
            paths.tmp_dir,
        ):
            ensure_dir(directory)
        return paths

    def to_dict(self, root: Path) -> dict[str, str]:
        return {
            "state_dir": relpath(self.state_dir, root),
            "logs_dir": relpath(self.logs_dir, root),
            "reports_dir": relpath(self.reports_dir, root),
            "artifacts_dir": relpath(self.artifacts_dir, root),
            "result_path": relpath(self.result_path, root),
            "latest_path": relpath(self.latest_path, root),
        }


@dataclass(slots=True)
class Context:
    root: Path
    config: Config
    run_id: str
    paths: RunPaths
    git: GitState
    clean_env: bool
    started_at: datetime
    source_date_epoch: int
    python_executable: Path
    base_env: dict[str, str] = field(default_factory=dict)
    tool_versions: dict[str, str] = field(default_factory=dict)
    extra_placeholders: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def profile(self) -> str:
        return self.config.policy.profile

    @property
    def version(self) -> str:
        return self.config.project.version or "unversioned"

    def render(self, value: str, *, extra: dict[str, str] | None = None) -> str:
        mapping = self._placeholders()
        if extra:
            mapping.update(extra)
        missing: list[str] = []

        def replacer(match: re.Match[str]) -> str:
            key = match.group(1)
            if key in mapping:
                return mapping[key]
            missing.append(key)
            return match.group(0)

        rendered = _PLACEHOLDER.sub(replacer, value)
        if missing:
            raise ConfigError(
                f"unknown placeholder(s) in {value!r}: {', '.join(sorted(set(missing)))}",
                remediation=(
                    "Known placeholders: " + ", ".join(sorted(mapping)) + ". "
                    "Use ${NAME} for shell environment variables."
                ),
            )
        return rendered

    def _placeholders(self) -> dict[str, str]:
        git_revision = self.git.revision or "nogit"
        branch = self.git.branch or "unknown"
        mapping = {
            "root": str(self.root),
            "revision": git_revision,
            "short_revision": self.git.short_revision or "nogit",
            "branch": branch,
            "project": self.config.project.name,
            "version": self.version,
            "run_id": self.run_id,
            "logs": str(self.paths.logs_dir),
            "reports": str(self.paths.reports_dir),
            "artifacts": str(self.paths.artifacts_dir),
            "state": str(self.paths.state_dir),
            "tmp": str(self.paths.tmp_dir),
            "source_date_epoch": str(self.source_date_epoch),
            "python": str(self.python_executable),
            "venv_python": str(self.python_executable),
            "release_dir": str(self.root / self.config.release.output_dir),
            "env": str(self.metadata.get("deploy_env", "")),
            "deploy_id": str(self.metadata.get("deploy_id", "")),
            "app": str(self.metadata.get("app_dir", "")),
            "base_url": str(self.metadata.get("base_url", "")),
        }
        mapping.update(self.extra_placeholders)
        return mapping

    def environment_snapshot(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": isoformat(self.started_at),
            "source_date_epoch": self.source_date_epoch,
            "profile": self.profile,
            "clean_env": self.clean_env,
            "platform": {
                "system": platform.system(),
                "release": platform.release(),
                "machine": platform.machine(),
                "python": platform.python_version(),
                "python_implementation": platform.python_implementation(),
                "executable": str(self.python_executable),
            },
            "environment": {
                "CI": self.base_env.get("CI"),
                "TZ": self.base_env.get("TZ"),
                "PYTHONHASHSEED": self.base_env.get("PYTHONHASHSEED"),
                "SOURCE_DATE_EPOCH": self.base_env.get("SOURCE_DATE_EPOCH"),
                "PATH_entries": len(self.base_env.get("PATH", "").split(os.pathsep)),
            },
            "tools": self.tool_versions,
            "git": self.git.to_dict(),
            "config_path": relpath(self.config.source_path, self.root),
            "paths": self.paths.to_dict(self.root),
        }

    def stage_env(
        self, stage: StageConfig, *, extra: dict[str, str] | None = None
    ) -> dict[str, str]:
        env = dict(self.base_env)
        stage_values = {key: self.render(value, extra=extra) for key, value in stage.env.items()}
        env.update(stage_values)
        if extra:
            env.update(extra)
        return env

    def stage_cwd(self, stage: StageConfig) -> Path:
        candidate = (self.root / stage.cwd).resolve()
        try:
            candidate.relative_to(self.root.resolve())
        except ValueError as exc:
            raise ConfigError(f"stage cwd escapes repository: {stage.cwd}") from exc
        if not candidate.is_dir():
            raise ConfigError(
                f"stage cwd does not exist: {stage.cwd}",
                remediation="Create the directory or fix [stages.<name>] cwd.",
            )
        return candidate


def _detect_python(root: Path) -> Path:
    candidates: list[Path] = [
        root / ".venv" / "bin" / "python",
        root / ".venv" / "Scripts" / "python.exe",
        root / "venv" / "bin" / "python",
        root / "venv" / "Scripts" / "python.exe",
    ]
    if sys.prefix != sys.base_prefix:
        # We are running inside a virtual environment. On layouts like uv's the
        # interpreter is a symlink and sys.executable may already be resolved to
        # the base interpreter, whose subprocesses lose the venv site-packages.
        # Prefer the venv's own launcher path so `{python}` keeps the venv.
        prefix = Path(sys.prefix)
        candidates.append(prefix / "bin" / "python")
        candidates.append(prefix / "Scripts" / "python.exe")
    candidates.append(Path(sys.executable))
    candidates.append(Path(shutil.which("python3") or ""))
    candidates.append(Path(shutil.which("python") or ""))
    for candidate in candidates:
        if candidate and candidate.exists():
            # A venv launcher is often a symlink; resolving it would strip the
            # venv context from every subprocess. Keep the launcher as-is when
            # it is one.
            if (candidate.parent.parent / "pyvenv.cfg").is_file():
                return candidate
            return candidate.resolve()
    raise PrerequisiteError(
        "no Python interpreter found",
        remediation="Install Python 3.11+ and make `python3` available on PATH.",
    )


def _git_state(root: Path) -> GitState:
    git_top = git_root(root)
    if git_top is None:
        return GitState(
            available=False,
            root=None,
            branch="",
            revision="",
            short_revision="",
            dirty=False,
            dirty_files=[],
            commit_epoch=None,
            tags_at_head=[],
            tracked_files=0,
        )
    branch = git_output(root, ["rev-parse", "--abbrev-ref", "HEAD"]) or "HEAD"
    revision = git_output(root, ["rev-parse", "HEAD"])
    short = git_output(root, ["rev-parse", "--short=12", "HEAD"])
    status = run_git(root, ["status", "--porcelain=v1", "--untracked-files=normal"])
    dirty_files = [
        line[3:].strip()
        for line in status.stdout.splitlines()
        if len(line) >= 4 and line[3:].strip()
    ]
    epoch_text = git_output(root, ["log", "-1", "--format=%ct"])
    try:
        epoch = int(epoch_text) if epoch_text else None
    except ValueError:
        epoch = None
    tags_text = git_output(root, ["tag", "--points-at", "HEAD"])
    tags = [tag for tag in tags_text.splitlines() if tag]
    tracked = git_output(root, ["ls-files"]).splitlines()
    return GitState(
        available=True,
        root=git_top,
        branch=branch,
        revision=revision,
        short_revision=short,
        dirty=bool(dirty_files),
        dirty_files=sorted(dirty_files),
        commit_epoch=epoch,
        tags_at_head=sorted(tags),
        tracked_files=len(tracked),
    )


def _base_environment(root: Path, clean_env: bool) -> dict[str, str]:
    if clean_env:
        allowed = {
            "PATH",
            "HOME",
            "TMPDIR",
            "TMP",
            "TEMP",
            "LANG",
            "LC_ALL",
            "TERM",
            "USER",
            "LOGNAME",
            "SHELL",
            "SSH_AUTH_SOCK",
            "XDG_CACHE_HOME",
            "VIRTUAL_ENV",
        }
        env = {key: value for key, value in os.environ.items() if key in allowed}
    else:
        env = dict(os.environ)
    path_entries = [
        str(root / ".venv" / "bin"),
        str(root / "venv" / "bin"),
        str(root / ".venv" / "Scripts"),
        str(root / "venv" / "Scripts"),
        str(root / "node_modules" / ".bin"),
        str(root / ".local" / "bin"),
    ]
    current_path = env.get("PATH", os.defpath)
    parts = [entry for entry in path_entries if Path(entry).is_dir()]
    parts.extend(part for part in current_path.split(os.pathsep) if part)
    seen: set[str] = set()
    ordered: list[str] = []
    for part in parts:
        if part not in seen:
            seen.add(part)
            ordered.append(part)
    env["PATH"] = os.pathsep.join(ordered)
    return env


def _tool_versions(environment: dict[str, str]) -> dict[str, str]:
    versions: dict[str, str] = {
        "python": f"{platform.python_version()} ({platform.python_implementation()})",
        "platform": f"{platform.system()} {platform.release()} {platform.machine()}",
        "shell": os.environ.get("SHELL", "/bin/sh"),
    }
    seen: set[str] = set()
    for tool in _TOOL_CANDIDATES:
        executable = shutil.which(tool, path=environment.get("PATH"))
        if not executable:
            if tool == "pip_audit":
                executable = shutil.which("pip-audit", path=environment.get("PATH"))
            if not executable:
                continue
        real = str(Path(executable).resolve())
        if real in seen:
            continue
        seen.add(real)
        command = [executable, "--version"]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=5,
                env=environment,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        output = (result.stdout or result.stderr).strip().splitlines()
        if output:
            versions[tool] = output[0].strip()[:200]
    return versions


def _source_date_epoch(git: GitState) -> int:
    override = os.environ.get("SOURCE_DATE_EPOCH")
    if override:
        try:
            value = int(override)
            if value < 0:
                raise ValueError
            return value
        except ValueError:
            raise ConfigError("SOURCE_DATE_EPOCH must be a non-negative integer") from None
    if git.commit_epoch is not None:
        return git.commit_epoch
    # 1980-01-01 is the minimum timestamp representable in a ZIP archive.
    return 315_532_800


def create_context(
    root: Path,
    config: Config,
    *,
    clean_env: bool = False,
) -> Context:
    root = root.resolve()
    config_root = config.root.resolve()
    if config_root != root:
        raise ConfigError(
            f"configuration root ({config_root}) does not match requested root ({root})"
        )
    git = _git_state(root)
    if git.available and git.root and git.root != root:
        raise ConfigError(
            f"{root} is inside the Git repository {git.root} but is not its root",
            remediation=(
                "ci-fabric currently requires ci.toml at the Git top level. "
                "Run the pipeline from the repository root."
            ),
        )
    if not git.available and not config.policy.allow_no_git:
        raise ConfigError(
            "this directory is not a Git repository",
            remediation=(
                "Initialise Git (`git init`) or set policy.allow_no_git=true for a "
                "deliberate non-Git workflow. Release and clean-tree gates stay unavailable."
            ),
        )
    python_executable = _detect_python(root)
    started = utc_now()
    revision = git.short_revision or "nogit"
    run_id = f"{started.strftime('%Y%m%dT%H%M%SZ')}-{revision}-{secrets.token_hex(3)}"
    paths = RunPaths.create(root, run_id)
    environment = _base_environment(root, clean_env)
    source_epoch = _source_date_epoch(git)
    deterministic = {
        "CI": "1",
        "LOCALCI": "1",
        "LOCALCI_RUN_ID": run_id,
        "LOCALCI_VERSION": "0.1.0",
        "LOCALCI_PROJECT": config.project.name,
        "LOCALCI_ARTIFACTS": str(paths.artifacts_dir),
        "LOCALCI_REPORTS": str(paths.reports_dir),
        "LOCALCI_LOGS": str(paths.logs_dir),
        "LOCALCI_STATE": str(paths.state_dir),
        "LOCALCI_PYTHON": str(python_executable),
        "TZ": "UTC",
        "PYTHONHASHSEED": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "SOURCE_DATE_EPOCH": str(source_epoch),
        "NPM_CONFIG_UPDATE_NOTIFIER": "false",
        "NO_COLOR": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    environment.update(deterministic)
    tool_versions = _tool_versions(environment)
    context = Context(
        root=root,
        config=config,
        run_id=run_id,
        paths=paths,
        git=git,
        clean_env=clean_env,
        started_at=started,
        source_date_epoch=source_epoch,
        python_executable=python_executable,
        base_env=environment,
        tool_versions=tool_versions,
    )
    context.environment_snapshot()  # validates serialisation early
    return context


def current_version() -> str:
    init = Path(__file__).with_name("__init__.py")
    try:
        text = read_text(init)
    except CiError:
        return "0.1.0"
    match = re.search(r'__version__ = "([^"]+)"', text)
    return match.group(1) if match else "0.1.0"


def print_context_summary(context: Context) -> str:
    git = context.git
    state = "dirty" if git.dirty else "clean"
    return (
        f"{context.config.project.name} {context.version} "
        f"@{git.short_revision or 'nogit'} ({git.branch or 'no-git'}, {state})"
    )
