"""Strict configuration loading for ``ci.toml``.

Configuration is intentionally explicit. Unknown keys are errors, not ignored:
a typo in a security or release setting must never silently disable a gate.
"""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError
from .util import load_toml, safe_relative_path

SCHEMA_VERSION = 1
PROFILES = ("minimal", "standard", "strict", "release")
STAGE_NAMES = (
    "repository",
    "format",
    "lint",
    "typecheck",
    "unit",
    "integration",
    "contract",
    "e2e",
    "coverage",
    "security",
    "audit",
    "build",
    "package",
    "artifact",
)
PIPELINE_STAGES = ("precheck", *STAGE_NAMES)
TEST_STAGES = ("unit", "integration", "contract", "e2e")
QUALITY_STAGES = ("format", "lint", "typecheck")
DEFAULT_TIMEOUTS: dict[str, int] = {
    "repository": 300,
    "format": 300,
    "lint": 600,
    "typecheck": 600,
    "unit": 900,
    "integration": 1200,
    "contract": 900,
    "e2e": 1800,
    "coverage": 300,
    "security": 600,
    "audit": 900,
    "build": 1800,
    "package": 900,
    "artifact": 900,
}
PROFILE_REQUIRED_STAGES: dict[str, tuple[str, ...]] = {
    "minimal": ("repository", "security"),
    "standard": ("repository", "security"),
    "strict": ("repository", "security", "audit"),
    "release": ("repository", "security", "audit"),
}
DEFAULT_FORBIDDEN_TRACKED = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_ed25519*",
    "*.keystore",
    ".netrc",
    ".DS_Store",
    "*.swp",
    "*.swo",
    "__pycache__/**",
    ".ci/**",
    ".localci/**",
    "node_modules/**",
    "venv/**",
    ".venv/**",
)
DEFAULT_PACKAGE_EXCLUDES = (
    ".git/**",
    ".ci/**",
    ".localci/**",
    ".env",
    ".env.*",
    "**/.env",
    "**/.env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "__pycache__/**",
    "**/__pycache__/**",
    "*.pyc",
    ".DS_Store",
    "node_modules/**",
    "**/node_modules/**",
    ".venv/**",
    "venv/**",
    "**/.venv/**",
)
SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
PEP440_RE = re.compile(r"^[0-9]+(\.[0-9]+)*((a|b|rc)[0-9]+)?(\.post[0-9]+)?(\.dev[0-9]+)?$")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@/-]*$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(slots=True)
class VersionSource:
    path: str
    format: str
    key: str | None = None
    regex: str | None = None


@dataclass(slots=True)
class ProjectConfig:
    name: str
    version: str | None = None
    version_file: str | None = None
    version_format: str = "semver"
    description: str = ""


@dataclass(slots=True)
class PolicyConfig:
    profile: str = "standard"
    fail_fast: bool = True
    require_clean_worktree: str = "release"
    keep_runs: int = 20
    required_stages: list[str] = field(default_factory=list)
    require_test_discovery: bool = True
    audit_required: bool = False
    allow_no_git: bool = False
    max_tracked_file_mib: float = 5.0
    # Run the contiguous block of test tiers (unit/integration/contract/e2e)
    # concurrently when two or more are selected. Results are reported in
    # canonical order either way; only wall-clock time changes.
    parallel_test_tiers: bool = False


@dataclass(slots=True)
class RepositoryConfig:
    required_paths: list[str] = field(default_factory=list)
    forbidden_paths: list[str] = field(default_factory=lambda: list(DEFAULT_FORBIDDEN_TRACKED))
    check_links: str = "warn"
    link_globs: list[str] = field(default_factory=lambda: ["**/*.md"])
    link_ignore: list[str] = field(default_factory=list)
    versions: list[VersionSource] = field(default_factory=list)
    generated_checks: list[str] = field(default_factory=list)
    required_lockfiles: list[str] = field(default_factory=list)
    max_tracked_file_mib: float = 5.0
    check_conflict_markers: bool = True
    check_license: bool = True


@dataclass(slots=True)
class StageConfig:
    name: str
    commands: list[str] = field(default_factory=list)
    fix_commands: list[str] = field(default_factory=list)
    required: bool = False
    enabled: bool = True
    timeout_seconds: int = 600
    cwd: str = "."
    env: dict[str, str] = field(default_factory=dict)
    min_test_files: int = 0
    min_tests: int = 0
    allow_empty: bool = False
    junit_path: str | None = None
    test_globs: list[str] = field(default_factory=list)
    report_path: str | None = None
    min_percent: float | None = None
    outputs: list[str] = field(default_factory=list)
    inputs: list[str] = field(default_factory=list)
    strategy: str = "archive"
    output: str | None = None
    archive_format: str = "tar.gz"
    strip_prefix: str | None = None
    exclude: list[str] = field(default_factory=list)
    smoke_commands: list[str] = field(default_factory=list)
    verify_archives: bool = True
    scan_secrets: bool = True
    must_contain: list[str] = field(default_factory=list)
    secret_scan: bool = True
    scan_untracked: bool = False
    scan_config: bool = True
    allowlist: list[str] = field(default_factory=list)
    # Committed baseline of accepted finding fingerprints (legacy debt):
    # new findings block, accepted ones are recorded as visible debt.
    baseline: str | None = None


@dataclass(slots=True)
class ReleaseConfig:
    require_clean: bool = True
    require_notes: bool = True
    changelog: str | None = "CHANGELOG.md"
    tag_prefix: str = "v"
    output_dir: str = ".localci/releases"
    # Bring-your-own signing: the command runs inside the finished release
    # bundle (cwd = bundle) and must exit 0; whatever it writes (for example
    # SHA256SUMS.sig) becomes part of the bundle. verify_command runs in the
    # same directory and must exit 0; deploy refuses unsigned-tampered bundles.
    sign_command: str | None = None
    verify_command: str | None = None


@dataclass(slots=True)
class DeployEnvConfig:
    name: str
    strategy: str = "process"
    port: int = 0
    host: str = "127.0.0.1"
    base_url: str | None = None
    health_path: str = "/health"
    readiness_path: str | None = None
    health_expect: str | None = None
    health_timeout: int = 30
    smoke_timeout: int = 120
    smoke_path: str = "/"
    smoke_expect: str | None = None
    smoke_commands: list[str] = field(default_factory=list)
    start_command: str | None = None
    server_command: str | None = None
    public_dir: str = "."
    app_subdir: str | None = None
    artifact_name: str | None = None
    stop_timeout: int = 10
    env: dict[str, str] = field(default_factory=dict)
    env_required: list[str] = field(default_factory=list)
    auto_rollback: bool = True


@dataclass(slots=True)
class HooksConfig:
    pre_commit_stages: list[str] = field(
        default_factory=lambda: ["repository", "format", "security"]
    )
    pre_push_stages: list[str] = field(
        default_factory=lambda: ["repository", "format", "lint", "typecheck", "unit"]
    )


@dataclass(slots=True)
class Config:
    root: Path
    source_path: Path
    project: ProjectConfig
    policy: PolicyConfig
    repository: RepositoryConfig
    stages: dict[str, StageConfig]
    release: ReleaseConfig
    deploy: dict[str, DeployEnvConfig]
    hooks: HooksConfig

    def stage(self, name: str) -> StageConfig | None:
        return self.stages.get(name)

    def stage_or_default(self, name: str) -> StageConfig:
        existing = self.stages.get(name)
        if existing is not None:
            return existing
        return StageConfig(name=name, timeout_seconds=DEFAULT_TIMEOUTS.get(name, 600))

    def effective_required_stages(self) -> list[str]:
        values = set(PROFILE_REQUIRED_STAGES[self.policy.profile])
        values.update(self.policy.required_stages)
        return sorted(values)

    def test_stage_names(self) -> list[str]:
        return [name for name in TEST_STAGES if name in self.stages]

    def resolved_version(self) -> str | None:
        return self.project.version

    def policy_document(self) -> dict[str, Any]:
        stages: dict[str, Any] = {}
        for name, stage in sorted(self.stages.items()):
            stages[name] = {
                "configured": True,
                "required": stage.required,
                "classification": "BLOCKING" if stage.required else "WARNING",
                "commands": len(stage.commands),
                "timeout_seconds": stage.timeout_seconds,
            }
        for name in STAGE_NAMES:
            if name not in stages:
                stages[name] = {
                    "configured": False,
                    "required": name in self.effective_required_stages(),
                    "classification": (
                        "BLOCKING" if name in self.effective_required_stages() else "SKIP"
                    ),
                }
        return {
            "schema_version": SCHEMA_VERSION,
            "profile": self.policy.profile,
            "fail_fast": self.policy.fail_fast,
            "required_stages": self.effective_required_stages(),
            "require_clean_worktree": self.policy.require_clean_worktree,
            "keep_runs": self.policy.keep_runs,
            "thresholds": {
                "max_tracked_file_mib": self.policy.max_tracked_file_mib,
                "coverage_percent": {
                    name: stage.min_percent
                    for name, stage in self.stages.items()
                    if stage.min_percent is not None
                },
            },
            "stages": stages,
        }


def _table(doc: dict[str, Any], key: str, where: str) -> dict[str, Any]:
    value = doc.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"{where}.{key} must be a table")
    return value


def _check_keys(table: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ConfigError(
            f"unknown key(s) in {where}: {', '.join(unknown)}",
            remediation=f"Allowed keys: {', '.join(sorted(allowed))}.",
        )


def _string(table: dict[str, Any], key: str, where: str, default: str | None = None) -> str | None:
    value = table.get(key, default)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{where}.{key} must be a string")
    return value


def _required_string(table: dict[str, Any], key: str, where: str) -> str:
    value = _string(table, key, where)
    if value is None or not value.strip():
        raise ConfigError(f"{where}.{key} is required and must not be empty")
    return value


def _boolean(table: dict[str, Any], key: str, where: str, default: bool) -> bool:
    value = table.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{where}.{key} must be true or false")
    return value


def _integer(
    table: dict[str, Any],
    key: str,
    where: str,
    default: int,
    *,
    minimum: int = 0,
    maximum: int = 86_400,
) -> int:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where}.{key} must be an integer")
    if not minimum <= value <= maximum:
        raise ConfigError(f"{where}.{key} must be between {minimum} and {maximum}")
    return int(value)


def _number(table: dict[str, Any], key: str, where: str, default: float | None) -> float | None:
    value = table.get(key, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where}.{key} must be a number")
    return float(value)


def _string_list(table: dict[str, Any], key: str, where: str) -> list[str]:
    value = table.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{where}.{key} must be a list of strings")
    return [item for item in value if item.strip()]


def _command_list(table: dict[str, Any], key: str, where: str) -> list[str]:
    values = _string_list(table, key, where)
    for value in values:
        if not value.strip():
            raise ConfigError(f"{where}.{key} contains an empty command")
    return values


def _env(table: dict[str, Any], key: str, where: str) -> dict[str, str]:
    value = table.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"{where}.{key} must be a table of strings")
    result: dict[str, str] = {}
    for name, item in value.items():
        if not isinstance(name, str) or not ENV_NAME_RE.match(name):
            raise ConfigError(f"{where}.{key} contains an invalid variable name: {name!r}")
        if not isinstance(item, str):
            raise ConfigError(f"{where}.{key}.{name} must be a string")
        result[name] = item
    return result


def _validate_relative(value: str, where: str) -> None:
    if value in {"", "."}:
        return
    try:
        safe_relative_path(value)
    except ConfigError as exc:
        raise ConfigError(
            f"{where} must be a relative path inside the repository: {value!r}"
        ) from exc


def _parse_versions(table: dict[str, Any], where: str) -> list[VersionSource]:
    raw = table.get("versions", [])
    if not isinstance(raw, list):
        raise ConfigError(f"{where}.versions must be a list of tables")
    sources: list[VersionSource] = []
    for index, item in enumerate(raw):
        item_where = f"{where}.versions[{index}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{item_where} must be a table")
        _check_keys(item, {"path", "format", "key", "regex"}, item_where)
        path = _required_string(item, "path", item_where)
        _validate_relative(path, f"{item_where}.path")
        fmt = _string(item, "format", item_where, "text") or "text"
        if fmt not in {"text", "toml", "json", "regex"}:
            raise ConfigError(f"{item_where}.format must be text, toml, json, or regex")
        key = _string(item, "key", item_where)
        pattern = _string(item, "regex", item_where)
        if fmt in {"toml", "json"} and not key:
            raise ConfigError(f"{item_where}.key is required for {fmt} version sources")
        if fmt == "regex" and not pattern:
            raise ConfigError(f"{item_where}.regex is required for regex version sources")
        if pattern is not None:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ConfigError(f"{item_where}.regex is invalid: {exc}") from exc
        sources.append(VersionSource(path=path, format=fmt, key=key, regex=pattern))
    return sources


def _parse_repository(doc: dict[str, Any], where: str) -> RepositoryConfig:
    allowed = {
        "required_paths",
        "forbidden_paths",
        "check_links",
        "link_globs",
        "link_ignore",
        "versions",
        "generated_checks",
        "required_lockfiles",
        "max_tracked_file_mib",
        "check_conflict_markers",
        "check_license",
    }
    _check_keys(doc, allowed, where)
    for key in ("required_paths", "forbidden_paths", "generated_checks"):
        for value in _string_list(doc, key, where):
            _validate_relative(value, f"{where}.{key}")
    check_links = _string(doc, "check_links", where, "warn") or "warn"
    if check_links not in {"off", "warn", "block"}:
        raise ConfigError(f"{where}.check_links must be off, warn, or block")
    generated = _string_list(doc, "generated_checks", where)
    max_size = _number(doc, "max_tracked_file_mib", where, 5.0)
    assert max_size is not None
    if max_size <= 0:
        raise ConfigError(f"{where}.max_tracked_file_mib must be greater than zero")
    return RepositoryConfig(
        required_paths=_string_list(doc, "required_paths", where),
        forbidden_paths=(
            _string_list(doc, "forbidden_paths", where)
            if "forbidden_paths" in doc
            else list(DEFAULT_FORBIDDEN_TRACKED)
        ),
        check_links=check_links,
        link_globs=_string_list(doc, "link_globs", where) or ["**/*.md"],
        link_ignore=_string_list(doc, "link_ignore", where),
        versions=_parse_versions(doc, where),
        generated_checks=generated,
        required_lockfiles=_string_list(doc, "required_lockfiles", where),
        max_tracked_file_mib=max_size,
        check_conflict_markers=_boolean(doc, "check_conflict_markers", where, True),
        check_license=_boolean(doc, "check_license", where, True),
    )


def _parse_stage(name: str, doc: dict[str, Any], where: str, policy: PolicyConfig) -> StageConfig:
    common = {
        "command",
        "commands",
        "fix_command",
        "fix_commands",
        "required",
        "enabled",
        "timeout",
        "cwd",
        "env",
    }
    specific: dict[str, set[str]] = {
        "format": {"fix_command", "fix_commands"},
        "unit": {"junit_path", "min_test_files", "min_tests", "allow_empty", "test_globs"},
        "integration": {"junit_path", "min_test_files", "min_tests", "allow_empty", "test_globs"},
        "contract": {"junit_path", "min_test_files", "min_tests", "allow_empty", "test_globs"},
        "e2e": {"junit_path", "min_test_files", "min_tests", "allow_empty", "test_globs"},
        "coverage": {"report_path", "min_percent"},
        "build": {"outputs"},
        "package": {
            "strategy",
            "inputs",
            "outputs",
            "output",
            "format",
            "strip_prefix",
            "exclude",
        },
        "artifact": {
            "smoke_commands",
            "verify_archives",
            "scan_secrets",
            "must_contain",
            "allowlist",
        },
        "security": {"secret_scan", "scan_untracked", "allowlist", "scan_config", "baseline"},
        "repository": set(),
        "audit": set(),
        "lint": set(),
        "typecheck": set(),
    }
    allowed = common | specific.get(name, set())
    _check_keys(doc, allowed, where)

    command_present = "command" in doc
    commands_present = "commands" in doc
    if command_present and commands_present:
        raise ConfigError(f"{where}: use either command or commands, not both")
    commands: list[str] = []
    if command_present:
        value = _string(doc, "command", where)
        if value is None or not value.strip():
            raise ConfigError(f"{where}.command must not be empty")
        commands = [value]
    elif commands_present:
        commands = _command_list(doc, "commands", where)
    fix_commands: list[str] = []
    if "fix_command" in doc and "fix_commands" in doc:
        raise ConfigError(f"{where}: use either fix_command or fix_commands, not both")
    if "fix_command" in doc:
        value = _string(doc, "fix_command", where)
        if value is not None and value.strip():
            fix_commands = [value]
    elif "fix_commands" in doc:
        fix_commands = _command_list(doc, "fix_commands", where)

    required = _boolean(doc, "required", where, False)
    enabled = _boolean(doc, "enabled", where, True)
    timeout = _integer(
        doc,
        "timeout",
        where,
        DEFAULT_TIMEOUTS.get(name, 600),
        minimum=1,
        maximum=86_400,
    )
    cwd = _string(doc, "cwd", where, ".") or "."
    _validate_relative(cwd, f"{where}.cwd")
    env = _env(doc, "env", where)

    min_percent = _number(doc, "min_percent", where, None)
    if min_percent is not None and not 0 <= min_percent <= 100:
        raise ConfigError(f"{where}.min_percent must be between 0 and 100")
    min_test_files = _integer(doc, "min_test_files", where, 0, maximum=1_000_000)
    min_tests = _integer(doc, "min_tests", where, 0, maximum=1_000_000)
    strategy = _string(doc, "strategy", where, "archive") or "archive"
    if strategy not in {"archive", "command"}:
        raise ConfigError(f"{where}.strategy must be archive or command")
    archive_format = _string(doc, "format", where, "tar.gz") or "tar.gz"
    if archive_format not in {"tar.gz", "tgz", "tar", "zip"}:
        raise ConfigError(f"{where}.format must be tar.gz, tar, or zip")
    output = _string(doc, "output", where)
    if output is not None:
        _validate_relative(output.replace("{artifacts}", ".ci/artifacts"), f"{where}.output")

    stage = StageConfig(
        name=name,
        commands=commands,
        fix_commands=fix_commands,
        required=required,
        enabled=enabled,
        timeout_seconds=timeout,
        cwd=cwd,
        env=env,
        min_test_files=min_test_files,
        min_tests=min_tests,
        allow_empty=_boolean(doc, "allow_empty", where, False),
        junit_path=_string(doc, "junit_path", where),
        test_globs=_string_list(doc, "test_globs", where),
        report_path=_string(doc, "report_path", where),
        min_percent=min_percent,
        outputs=_string_list(doc, "outputs", where),
        inputs=_string_list(doc, "inputs", where),
        strategy=strategy,
        output=output,
        archive_format="tar.gz" if archive_format == "tgz" else archive_format,
        strip_prefix=_string(doc, "strip_prefix", where),
        exclude=_string_list(doc, "exclude", where),
        smoke_commands=_string_list(doc, "smoke_commands", where),
        verify_archives=_boolean(doc, "verify_archives", where, True),
        scan_secrets=_boolean(doc, "scan_secrets", where, True),
        must_contain=_string_list(doc, "must_contain", where),
        secret_scan=_boolean(doc, "secret_scan", where, True),
        scan_untracked=_boolean(doc, "scan_untracked", where, False),
        scan_config=_boolean(doc, "scan_config", where, True),
        allowlist=_string_list(doc, "allowlist", where),
        baseline=_string(doc, "baseline", where),
    )
    _validate_stage(stage, policy)
    return stage


def _validate_stage(stage: StageConfig, policy: PolicyConfig) -> None:
    where = f"stages.{stage.name}"
    if stage.name in TEST_STAGES and stage.junit_path:
        _validate_relative(
            stage.junit_path.replace("{artifacts}", ".ci/artifacts"),
            f"{where}.junit_path",
        )
    if stage.name == "coverage" and stage.report_path:
        _validate_relative(
            stage.report_path.replace("{artifacts}", ".ci/artifacts"),
            f"{where}.report_path",
        )
    if stage.name == "security" and not (stage.commands or stage.secret_scan or stage.scan_config):
        raise ConfigError(f"{where}: security stage has no work to do")
    needs_command = stage.name in {
        "format",
        "lint",
        "typecheck",
        "unit",
        "integration",
        "contract",
        "e2e",
        "coverage",
        "audit",
        "build",
    }
    if stage.name in {"format", "lint", "typecheck"} and stage.required and not stage.commands:
        raise ConfigError(f"{where}.required is true but no command is configured")
    if stage.name in TEST_STAGES and stage.required and not stage.commands:
        raise ConfigError(f"{where}.required is true but no command is configured")
    if stage.name == "coverage" and stage.min_percent is not None and not stage.commands:
        raise ConfigError(f"{where}.min_percent requires a coverage command")
    if stage.name == "audit" and stage.required and not stage.commands:
        raise ConfigError(f"{where}.required is true but no audit command is configured")
    if stage.name == "build" and stage.required and not stage.commands:
        raise ConfigError(f"{where}.required is true but no build command is configured")
    if stage.name == "package":
        if stage.required and stage.strategy == "command" and not stage.commands:
            raise ConfigError(
                f"{where}.required is true but package.strategy=command has no command"
            )
        if stage.strategy == "archive" and stage.required and not stage.output:
            raise ConfigError(f"{where}.output is required for an archive package")
    if policy.audit_required and stage.name == "audit" and not stage.commands:
        raise ConfigError(
            f"{where}: profile '{policy.profile}' requires a dependency audit command",
            remediation="Configure [stages.audit] command or use profile=standard/minimal.",
        )
    if needs_command and stage.required and not stage.commands:
        raise ConfigError(f"{where}.required is true but no command is configured")


def _parse_policy(doc: dict[str, Any], where: str) -> PolicyConfig:
    allowed = {
        "profile",
        "fail_fast",
        "require_clean_worktree",
        "keep_runs",
        "parallel_test_tiers",
        "required_stages",
        "require_test_discovery",
        "audit_required",
        "allow_no_git",
        "max_tracked_file_mib",
    }
    _check_keys(doc, allowed, where)
    profile = _string(doc, "profile", where, "standard") or "standard"
    if profile not in PROFILES:
        raise ConfigError(
            f"{where}.profile must be one of {', '.join(PROFILES)}",
            remediation="Fix the profile name in ci.toml, or regenerate the file with "
            "`./ci init --force` and review the diff.",
        )
    require_clean = _string(doc, "require_clean_worktree", where, "release") or "release"
    if require_clean not in {"never", "release", "always"}:
        raise ConfigError(f"{where}.require_clean_worktree must be never, release, or always")
    required_stages = _string_list(doc, "required_stages", where)
    unknown = sorted(set(required_stages) - set(STAGE_NAMES))
    if unknown:
        raise ConfigError(f"{where}.required_stages contains unknown stages: {', '.join(unknown)}")
    max_size = _number(doc, "max_tracked_file_mib", where, 5.0)
    assert max_size is not None
    if max_size <= 0:
        raise ConfigError(f"{where}.max_tracked_file_mib must be greater than zero")
    audit_required = _boolean(doc, "audit_required", where, profile in {"strict", "release"})
    return PolicyConfig(
        profile=profile,
        fail_fast=_boolean(doc, "fail_fast", where, True),
        parallel_test_tiers=_boolean(doc, "parallel_test_tiers", where, False),
        require_clean_worktree=require_clean,
        keep_runs=_integer(doc, "keep_runs", where, 20, minimum=1, maximum=10_000),
        required_stages=required_stages,
        require_test_discovery=_boolean(doc, "require_test_discovery", where, True),
        audit_required=audit_required,
        allow_no_git=_boolean(doc, "allow_no_git", where, False),
        max_tracked_file_mib=max_size,
    )


def _parse_release(doc: dict[str, Any], where: str) -> ReleaseConfig:
    _check_keys(
        doc,
        {
            "require_clean",
            "require_notes",
            "changelog",
            "tag_prefix",
            "output_dir",
            "sign_command",
            "verify_command",
        },
        where,
    )
    output_dir = _string(doc, "output_dir", where, ".localci/releases") or ".localci/releases"
    _validate_relative(output_dir, f"{where}.output_dir")
    tag_prefix = _string(doc, "tag_prefix", where, "v") or ""
    if tag_prefix and not re.match(r"^[A-Za-z0-9._/-]+$", tag_prefix):
        raise ConfigError(f"{where}.tag_prefix contains unsupported characters")
    changelog = _string(doc, "changelog", where, "CHANGELOG.md")
    if changelog is not None:
        _validate_relative(changelog, f"{where}.changelog")
    sign_command = _string(doc, "sign_command", where)
    verify_command = _string(doc, "verify_command", where)
    if not sign_command and verify_command:
        raise ConfigError(
            f"{where}.verify_command is configured without sign_command; a release "
            "cannot be verified against a signature nobody is producing",
            remediation="Set [release].sign_command, or remove verify_command.",
        )
    return ReleaseConfig(
        require_clean=_boolean(doc, "require_clean", where, True),
        require_notes=_boolean(doc, "require_notes", where, True),
        changelog=changelog,
        tag_prefix=tag_prefix,
        output_dir=output_dir,
        sign_command=sign_command,
        verify_command=verify_command,
    )


def _parse_deploy_env(name: str, doc: dict[str, Any], where: str) -> DeployEnvConfig:
    allowed = {
        "strategy",
        "port",
        "host",
        "base_url",
        "health_path",
        "readiness_path",
        "health_expect",
        "health_timeout",
        "smoke_timeout",
        "smoke_path",
        "smoke_expect",
        "smoke_commands",
        "start_command",
        "server_command",
        "public_dir",
        "app_subdir",
        "artifact_name",
        "stop_timeout",
        "env",
        "env_required",
        "auto_rollback",
    }
    _check_keys(doc, allowed, where)
    strategy = _string(doc, "strategy", where, "process") or "process"
    if strategy not in {"process", "static"}:
        raise ConfigError(f"{where}.strategy must be process or static")
    start_command = _string(doc, "start_command", where)
    if strategy == "process" and not start_command:
        raise ConfigError(f"{where}.start_command is required for strategy=process")
    host = _string(doc, "host", where, "127.0.0.1") or "127.0.0.1"
    port = _integer(doc, "port", where, 0, minimum=0, maximum=65_535)
    environment = _env(doc, "env", where)
    for key, value in environment.items():
        if re.match(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$", value):
            continue
        if re.search(r"(?i)(secret|token|password|passwd|key|credential)", key):
            raise ConfigError(
                f"{where}.env.{key} looks secret but is a literal",
                remediation=f'Use {key} = "${{{key}}}" so the value comes from the environment.',
            )
    health_path = _string(
        doc, "health_path", where, "/health" if strategy == "process" else "/"
    ) or ("/health" if strategy == "process" else "/")
    readiness_path = _string(doc, "readiness_path", where)
    smoke_path = _string(doc, "smoke_path", where, "/") or "/"
    for path_name, path_value in (
        ("health_path", health_path),
        ("readiness_path", readiness_path),
        ("smoke_path", smoke_path),
    ):
        if path_value is not None and not path_value.startswith("/"):
            raise ConfigError(f"{where}.{path_name} must start with '/'")
    public_dir = _string(doc, "public_dir", where, ".") or "."
    _validate_relative(public_dir, f"{where}.public_dir")
    app_subdir = _string(doc, "app_subdir", where)
    if app_subdir is not None:
        _validate_relative(app_subdir, f"{where}.app_subdir")
    env_required = _string_list(doc, "env_required", where)
    for item in env_required:
        if not ENV_NAME_RE.match(item):
            raise ConfigError(f"{where}.env_required contains an invalid name: {item!r}")
    return DeployEnvConfig(
        name=name,
        strategy=strategy,
        port=port,
        host=host,
        base_url=_string(doc, "base_url", where),
        health_path=health_path,
        readiness_path=readiness_path,
        health_expect=_string(doc, "health_expect", where),
        health_timeout=_integer(doc, "health_timeout", where, 30, minimum=1, maximum=3600),
        smoke_timeout=_integer(doc, "smoke_timeout", where, 120, minimum=1, maximum=3600),
        smoke_path=smoke_path,
        smoke_expect=_string(doc, "smoke_expect", where),
        smoke_commands=_string_list(doc, "smoke_commands", where),
        start_command=start_command,
        server_command=_string(doc, "server_command", where),
        public_dir=public_dir,
        app_subdir=app_subdir,
        artifact_name=_string(doc, "artifact_name", where),
        stop_timeout=_integer(doc, "stop_timeout", where, 10, minimum=1, maximum=300),
        env=environment,
        env_required=env_required,
        auto_rollback=_boolean(doc, "auto_rollback", where, True),
    )


def _parse_hooks(doc: dict[str, Any], where: str) -> HooksConfig:
    _check_keys(doc, {"pre_commit_stages", "pre_push_stages"}, where)
    defaults = HooksConfig()
    pre_commit = _string_list(doc, "pre_commit_stages", where) or defaults.pre_commit_stages
    pre_push = _string_list(doc, "pre_push_stages", where) or defaults.pre_push_stages
    for label, values in (("pre_commit_stages", pre_commit), ("pre_push_stages", pre_push)):
        unknown = sorted(set(values) - set(STAGE_NAMES) - {"precheck"})
        if unknown:
            raise ConfigError(f"{where}.{label} contains unknown stages: {', '.join(unknown)}")
    return HooksConfig(pre_commit_stages=pre_commit, pre_push_stages=pre_push)


def _read_version_file(root: Path, relative: str) -> str:
    try:
        path = (root / relative).resolve()
        path.relative_to(root.resolve())
    except (OSError, ValueError) as exc:
        raise ConfigError(f"version_file escapes the repository: {relative}") from exc
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise ConfigError(f"version_file does not exist: {relative}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read version_file {relative}: {exc}") from exc
    if not value:
        raise ConfigError(f"version_file is empty: {relative}")
    return value.splitlines()[0].strip()


def _validate_required_configuration(config: Config) -> None:
    profile = config.policy.profile
    for name in config.effective_required_stages():
        if name == "repository":
            continue
        stage = config.stages.get(name)
        if name == "security":
            if stage is not None and not stage.enabled:
                raise ConfigError(
                    f"profile '{profile}' requires the security stage, but it is disabled"
                )
            if stage is not None and not (stage.commands or stage.secret_scan or stage.scan_config):
                raise ConfigError(
                    f"profile '{profile}' requires the security stage, but it has no work to do"
                )
            continue
        if stage is None:
            raise ConfigError(
                f"profile '{profile}' requires [stages.{name}], but it is not configured",
                remediation=(
                    f"Add [stages.{name}] with a command, or remove '{name}' from "
                    "policy.required_stages and use a profile that does not require it."
                ),
            )
        if not stage.enabled:
            raise ConfigError(f"profile '{profile}' requires the {name} stage, but it is disabled")
        if name == "audit" and not stage.commands:
            raise ConfigError(
                f"profile '{profile}' requires a dependency audit, but no command is configured",
                remediation="Add [stages.audit] command, or use profile=standard.",
            )
        if not stage.commands and name not in {"package"}:
            raise ConfigError(
                f"profile '{profile}' requires the {name} stage, but it has no command",
                remediation=f"Add [stages.{name}] command, or change the policy profile.",
            )


def load_config(root: Path, config_path: Path | None = None) -> Config:
    root = root.resolve()
    source = config_path or (root / "ci.toml")
    if not source.is_absolute():
        source = (root / source).resolve()
    if not source.is_file():
        raise ConfigError(
            f"CI configuration not found: {source}",
            remediation="Run `./ci init` (or `ci-fabric init`) to create ci.toml.",
        )
    doc = load_toml(source)
    _check_keys(
        doc,
        {"version", "project", "policy", "repository", "stages", "release", "deploy", "hooks"},
        "ci.toml",
    )
    version = doc.get("version")
    if version != SCHEMA_VERSION:
        raise ConfigError(
            f"ci.toml version must be {SCHEMA_VERSION} (found {version!r})",
            remediation="Regenerate the file with `./ci init --force` and review the diff.",
        )

    project_table = _table(doc, "project", "ci.toml")
    _check_keys(
        project_table,
        {"name", "version", "version_file", "version_format", "description"},
        "project",
    )
    name = _string(project_table, "name", "project", root.name) or root.name
    if not NAME_RE.match(name):
        raise ConfigError(
            "project.name must start with an alphanumeric character and contain only "
            "letters, digits, '.', '_', '@', '/', or '-'"
        )
    version_file = _string(project_table, "version_file", "project")
    if version_file is not None:
        _validate_relative(version_file, "project.version_file")
    literal_version = _string(project_table, "version", "project")
    resolved_version = literal_version
    if version_file:
        file_version = _read_version_file(root, version_file)
        if literal_version is not None and literal_version != file_version:
            raise ConfigError(
                f"project.version ({literal_version}) does not match "
                f"{version_file} ({file_version})"
            )
        resolved_version = file_version
    version_format = _string(project_table, "version_format", "project", "semver") or "semver"
    if version_format not in {"semver", "pep440", "none"}:
        raise ConfigError("project.version_format must be semver, pep440, or none")
    if resolved_version is not None and version_format != "none":
        if version_format == "semver" and not validate_semver(resolved_version):
            raise ConfigError(
                f"project version is not valid semver: {resolved_version!r}",
                remediation="Fix the version source or set project.version_format='none'.",
            )
        if version_format == "pep440" and not validate_pep440(resolved_version):
            raise ConfigError(
                f"project version is not valid PEP 440: {resolved_version!r}",
                remediation="Fix the version source or set project.version_format='none'.",
            )
    project = ProjectConfig(
        name=name,
        version=resolved_version,
        version_file=version_file,
        version_format=version_format,
        description=_string(project_table, "description", "project", "") or "",
    )

    policy = _parse_policy(_table(doc, "policy", "ci.toml"), "policy")
    repository = _parse_repository(_table(doc, "repository", "ci.toml"), "repository")

    stages_doc = _table(doc, "stages", "ci.toml")
    unknown_stages = sorted(set(stages_doc) - set(STAGE_NAMES))
    if unknown_stages:
        raise ConfigError(
            f"unknown stage table(s): {', '.join(unknown_stages)}",
            remediation=f"Known stages: {', '.join(STAGE_NAMES)}.",
        )
    stages: dict[str, StageConfig] = {}
    for stage_name, stage_doc in stages_doc.items():
        if not isinstance(stage_doc, dict):
            raise ConfigError(f"stages.{stage_name} must be a table")
        stages[stage_name] = _parse_stage(stage_name, stage_doc, f"stages.{stage_name}", policy)

    release = _parse_release(_table(doc, "release", "ci.toml"), "release")

    deploy_doc = _table(doc, "deploy", "ci.toml")
    deploy: dict[str, DeployEnvConfig] = {}
    for env_name, env_doc in deploy_doc.items():
        if not re.match(r"^[a-z0-9][a-z0-9_-]*$", env_name):
            raise ConfigError(f"deploy environment name is invalid: {env_name!r}")
        if not isinstance(env_doc, dict):
            raise ConfigError(f"deploy.{env_name} must be a table")
        deploy[env_name] = _parse_deploy_env(env_name, env_doc, f"deploy.{env_name}")

    hooks = _parse_hooks(_table(doc, "hooks", "ci.toml"), "hooks")

    config = Config(
        root=root,
        source_path=source,
        project=project,
        policy=policy,
        repository=repository,
        stages=stages,
        release=release,
        deploy=deploy,
        hooks=hooks,
    )
    _validate_required_configuration(config)
    return config


def validate_semver(value: str) -> bool:
    return bool(SEMVER_RE.match(value))


def validate_pep440(value: str) -> bool:
    return bool(PEP440_RE.match(value))


def expand_env_value(value: str, environment: Mapping[str, str] | None = None) -> str:
    """Expand ``${NAME}`` references in deploy environment values.

    Values without a reference are literals and are validated at config load.
    Missing variables are a deployment error, never an empty string.
    """
    source = environment if environment is not None else os.environ

    def replacer(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in source:
            raise ConfigError(
                f"deployment environment variable is not set: {name}",
                remediation=f"Export {name} before deploying; ci-fabric never stores secrets.",
            )
        return source[name]

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", replacer, value)


def command_program(command: str) -> str | None:
    """Best-effort executable name for doctor output."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    for token in tokens:
        if "=" in token and not token.startswith("-"):
            continue
        return token
    return None
