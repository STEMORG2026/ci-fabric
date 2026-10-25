"""Stack detection used by ``./ci init``.

Detection is intentionally conservative: it only generates commands that map to
files or scripts that actually exist. It never installs dependencies and never
executes project code.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import PEP440_RE, SEMVER_RE
from .errors import ConfigError

_LOCKFILES = (
    "uv.lock",
    "poetry.lock",
    "requirements-dev.lock",
    "requirements.lock",
    "requirements.txt",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lockb",
    "Cargo.lock",
    "go.sum",
)
_NODE_SCRIPTS = {
    "format": ("format:check", "format-check", "prettier:check"),
    "lint": ("lint", "eslint"),
    "typecheck": ("typecheck", "type-check", "tsc"),
    "unit": ("test", "test:unit"),
    "integration": ("test:integration", "integration"),
    "e2e": ("test:e2e", "e2e"),
    "build": ("build",),
}


@dataclass(slots=True)
class Stack:
    name: str
    commands: dict[str, list[str]] = field(default_factory=dict)
    audit: list[str] = field(default_factory=list)
    test_globs: list[str] = field(default_factory=list)
    build_outputs: list[str] = field(default_factory=list)
    package_inputs: list[str] = field(default_factory=list)
    package_strip_prefix: str | None = None
    package_must_contain: list[str] = field(default_factory=list)
    junit_path: str | None = None
    coverage_report: str | None = None
    coverage_min_percent: float | None = None
    notes: list[str] = field(default_factory=list)

    def add(self, stage: str, command: str) -> None:
        values = self.commands.setdefault(stage, [])
        if command not in values:
            values.append(command)


@dataclass(slots=True)
class Detection:
    root: Path
    name: str
    version: str | None
    version_file: str | None
    version_format: str
    version_sources: list[dict[str, str]] = field(default_factory=list)
    stacks: list[Stack] = field(default_factory=list)
    lockfiles: list[str] = field(default_factory=list)
    commands: dict[str, list[str]] = field(default_factory=dict)
    audits: list[str] = field(default_factory=list)
    test_globs: list[str] = field(default_factory=list)
    build_outputs: list[str] = field(default_factory=list)
    package_inputs: list[str] = field(default_factory=list)
    package_strip_prefix: str | None = None
    package_must_contain: list[str] = field(default_factory=list)
    junit_path: str | None = None
    coverage_report: str | None = None
    coverage_min_percent: float | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def language_names(self) -> list[str]:
        return [stack.name for stack in self.stacks]

    def has(self, name: str) -> bool:
        return any(stack.name == name for stack in self.stacks)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _version_paths(root: Path) -> list[dict[str, str]]:
    sources: list[dict[str, str]] = []
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        data = _read_toml(pyproject)
        project = data.get("project")
        if isinstance(project, dict) and isinstance(project.get("version"), str):
            sources.append({"path": "pyproject.toml", "format": "toml", "key": "project.version"})
    package_json = root / "package.json"
    if package_json.is_file():
        data = _read_json(package_json)
        if isinstance(data.get("version"), str):
            sources.append({"path": "package.json", "format": "json", "key": "version"})
    cargo = root / "Cargo.toml"
    if cargo.is_file():
        data = _read_toml(cargo)
        package = data.get("package")
        if isinstance(package, dict) and isinstance(package.get("version"), str):
            sources.append({"path": "Cargo.toml", "format": "toml", "key": "package.version"})
    return sources


def _choose_name(root: Path) -> str:
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        project = _read_toml(pyproject).get("project")
        if isinstance(project, dict) and isinstance(project.get("name"), str):
            return str(project["name"]).rsplit("/", 1)[-1]
    package_json = root / "package.json"
    if package_json.is_file():
        name = _read_json(package_json).get("name")
        if isinstance(name, str) and name:
            return name.rsplit("/", 1)[-1]
    cargo = _read_toml(root / "Cargo.toml") if (root / "Cargo.toml").is_file() else {}
    package = cargo.get("package")
    if isinstance(package, dict) and isinstance(package.get("name"), str):
        return str(package["name"])
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", root.name).strip("-.")
    return cleaned or "project"


def _version_format(value: str | None) -> str:
    if value is None:
        return "none"
    if SEMVER_RE.match(value):
        return "semver"
    if PEP440_RE.match(value):
        return "pep440"
    return "none"


def _detect_python(root: Path, stack: Stack) -> None:
    pyproject = root / "pyproject.toml"
    data = _read_toml(pyproject) if pyproject.is_file() else {}
    tool = data.get("tool") if isinstance(data.get("tool"), dict) else {}
    has_tests = (root / "tests").is_dir() or (root / "test").is_dir()
    pytest_config = isinstance(tool.get("pytest"), dict) if tool else False
    coverage_config = isinstance(tool.get("coverage"), dict) if tool else False
    ruff = isinstance(tool.get("ruff"), dict) if tool else (root / "ruff.toml").is_file()
    mypy = isinstance(tool.get("mypy"), dict) if tool else (root / "mypy.ini").is_file()
    if ruff:
        stack.add("format", "{python} -m ruff format --check .")
        stack.add("lint", "{python} -m ruff check .")
    if mypy:
        stack.add("typecheck", "{python} -m mypy .")
    requirements = root / "requirements.txt"
    if has_tests or pytest_config:
        junit = "{artifacts}/junit-unit.xml"
        command = f"{{python}} -m pytest -q --junitxml={junit}"
        if coverage_config:
            report = "{artifacts}/coverage.xml"
            command += f" --cov --cov-report=xml:{report}"
            stack.coverage_report = report
            coverage_table = tool.get("coverage") if tool else None
            fail_under: object = None
            if isinstance(coverage_table, dict):
                report_table = coverage_table.get("report")
                if isinstance(report_table, dict):
                    fail_under = report_table.get("fail_under")
            if isinstance(fail_under, (int, float)) and not isinstance(fail_under, bool):
                stack.coverage_min_percent = float(fail_under)
        stack.add("unit", command)
        stack.junit_path = junit
    if isinstance(data.get("build-system"), dict):
        stack.add("build", "{python} -m build")
        stack.build_outputs = ["dist"]
        stack.package_inputs = ["dist"]
        stack.package_strip_prefix = "dist"
    if requirements.is_file():
        stack.audit.append("{python} -m pip_audit -r requirements.txt")
    stack.test_globs = ["tests/**/*.py", "test_*/**/*.py"]


def _node_runner(root: Path) -> str:
    if (root / "pnpm-lock.yaml").is_file():
        return "pnpm"
    if (root / "yarn.lock").is_file():
        return "yarn"
    if (root / "bun.lockb").is_file():
        return "bun"
    return "npm"


def _detect_node(root: Path, stack: Stack) -> None:
    package_json = root / "package.json"
    if not package_json.is_file():
        return
    data = _read_json(package_json)
    scripts = data.get("scripts")
    if not isinstance(scripts, dict):
        scripts = {}
    runner = _node_runner(root)
    run_prefix = f"{runner} run" if runner != "npm" else "npm run"
    for stage, candidates in _NODE_SCRIPTS.items():
        for candidate in candidates:
            if isinstance(scripts.get(candidate), str):
                command = (
                    f"{runner} test"
                    if candidate == "test" and runner in {"npm", "yarn", "pnpm"}
                    else f"{run_prefix} {candidate}"
                )
                stack.add(stage, command)
                break
    has_vite = any(root.glob("vite.config.*")) or any(root.glob("vitest.config.*"))
    if "build" in stack.commands and (has_vite or (root / "dist").exists()):
        stack.build_outputs = ["dist"]
        stack.package_inputs = ["dist"]
        stack.package_strip_prefix = "dist"
        if isinstance(scripts.get("build"), str) and "vite" in scripts["build"]:
            stack.package_must_contain = ["index.html"]
    if (root / "package-lock.json").is_file() or (root / "pnpm-lock.yaml").is_file():
        stack.audit.append(
            "npm audit --audit-level=high"
            if runner == "npm"
            else f"{runner} audit --audit-level=high"
        )
    stack.test_globs = [
        "**/*.test.ts",
        "**/*.test.tsx",
        "**/*.test.js",
        "**/*.test.jsx",
        "**/*.spec.ts",
        "**/*.spec.tsx",
        "**/*.spec.js",
        "**/*.spec.jsx",
    ]


def _detect_go(root: Path, stack: Stack) -> None:
    stack.add("format", 'test -z "$(gofmt -l .)"')
    stack.add("lint", "go vet ./...")
    stack.add("unit", "go test ./...")
    stack.add("build", "go build -o dist/ ./...")
    stack.build_outputs = ["dist"]
    stack.package_inputs = ["dist"]
    stack.package_strip_prefix = "dist"
    stack.audit.append("govulncheck ./...")
    stack.test_globs = ["**/*_test.go"]


def _detect_rust(root: Path, stack: Stack) -> None:
    stack.add("format", "cargo fmt --all -- --check")
    stack.add("lint", "cargo clippy --all-targets -- -D warnings")
    stack.add("unit", "cargo test --all")
    stack.add("build", "cargo build --release")
    data = _read_toml(root / "Cargo.toml")
    package = data.get("package") if isinstance(data.get("package"), dict) else {}
    name = package.get("name") if isinstance(package, dict) else None
    if isinstance(name, str) and name:
        stack.build_outputs = [f"target/release/{name}"]
        stack.package_inputs = [f"target/release/{name}"]
    stack.audit.append("cargo audit")
    stack.test_globs = ["**/*.rs"]


def _detect_dotnet(root: Path, stack: Stack) -> None:
    stack.add("format", "dotnet format --verify-no-changes")
    stack.add("lint", "dotnet build -c Release --nologo --warnaserror")
    stack.add("unit", "dotnet test -c Release --nologo")
    stack.add("build", "dotnet build -c Release --nologo")
    stack.test_globs = ["**/*.test.cs"]


def _detect_static(root: Path, stack: Stack) -> None:
    public_index = root / "public" / "index.html"
    root_index = root / "index.html"
    if public_index.is_file():
        stack.package_inputs = ["public"]
        stack.package_strip_prefix = "public"
        stack.package_must_contain = ["index.html"]
    elif root_index.is_file():
        inputs = ["index.html"]
        inputs.extend(
            name for name in ("assets", "css", "js", "images", "fonts") if (root / name).is_dir()
        )
        stack.package_inputs = inputs
        stack.package_must_contain = ["index.html"]
    else:
        return
    stack.notes.append("Static content: format/lint/typecheck/test are not applicable.")


def detect_project(root: Path) -> Detection:
    root = root.resolve()
    name = _choose_name(root)
    sources = _version_paths(root)
    version: str | None = None
    version_file: str | None = None
    version_source = root / "VERSION"
    if version_source.is_file():
        first = version_source.read_text(encoding="utf-8").strip().splitlines()
        if first:
            version = first[0].strip()
            version_file = "VERSION"
    if version is None and sources:
        version = _read_source_version(root, sources[0])
    version_format = _version_format(version)
    if version_file:
        sources = [source for source in sources if source["path"] != version_file]
    detection = Detection(
        root=root,
        name=name,
        version=version,
        version_file=version_file,
        version_format=version_format,
        version_sources=sources,
    )
    detection.lockfiles = [name_ for name_ in _LOCKFILES if (root / name_).is_file()]

    if (
        (root / "pyproject.toml").is_file()
        or (root / "requirements.txt").is_file()
        or (root / "setup.py").is_file()
    ):
        stack = Stack(name="python")
        _detect_python(root, stack)
        if stack.commands:
            detection.stacks.append(stack)
    if (root / "package.json").is_file():
        stack = Stack(name="node")
        _detect_node(root, stack)
        if stack.commands:
            detection.stacks.append(stack)
    if (root / "go.mod").is_file():
        stack = Stack(name="go")
        _detect_go(root, stack)
        detection.stacks.append(stack)
    if (root / "Cargo.toml").is_file():
        stack = Stack(name="rust")
        _detect_rust(root, stack)
        detection.stacks.append(stack)
    if any(root.glob("*.csproj")) or any(root.glob("*.sln")):
        stack = Stack(name="dotnet")
        _detect_dotnet(root, stack)
        detection.stacks.append(stack)
    if (root / "Assets").is_dir() and (root / "ProjectSettings").is_dir():
        detection.stacks.append(
            Stack(
                name="unity",
                notes=["Unity project detected; CI does not invoke the Unity editor."],
            )
        )
    static = Stack(name="static")
    _detect_static(root, static)
    if static.package_inputs:
        detection.stacks.append(static)

    for stack in detection.stacks:
        for stage, commands in stack.commands.items():
            detection.commands.setdefault(stage, []).extend(commands)
        detection.audits.extend(stack.audit)
        detection.test_globs.extend(stack.test_globs)
        detection.notes.extend(stack.notes)
        if stack.package_inputs and not detection.package_inputs:
            detection.package_inputs = stack.package_inputs
            detection.package_strip_prefix = stack.package_strip_prefix
            detection.package_must_contain = stack.package_must_contain
        if stack.build_outputs and not detection.build_outputs:
            detection.build_outputs = stack.build_outputs
        if stack.junit_path and not detection.junit_path:
            detection.junit_path = stack.junit_path
        if stack.coverage_report and not detection.coverage_report:
            detection.coverage_report = stack.coverage_report
            detection.coverage_min_percent = stack.coverage_min_percent
    detection.test_globs = sorted(set(detection.test_globs))
    if not detection.stacks:
        detection.notes.append(
            "No supported stack markers were found; configure commands manually."
        )
    return detection


def _read_source_version(root: Path, source: dict[str, str]) -> str | None:
    path = root / source["path"]
    if source["format"] == "toml":
        data = _read_toml(path)
        current: Any = data
        for part in source.get("key", "").split("."):
            if not isinstance(current, dict) or part not in current:
                return None
            current = current[part]
        return str(current) if current is not None else None
    if source["format"] == "json":
        data = _read_json(path)
        current = data
        for part in source.get("key", "").split("."):
            if not isinstance(current, dict) or part not in current:
                return None
            current = current[part]
        return str(current) if current is not None else None
    return None


def require_audit_command(detection: Detection, profile: str) -> None:
    if profile in {"strict", "release"} and not detection.audits:
        raise ConfigError(
            f"profile '{profile}' requires a dependency audit, but no supported lockfile "
            "was detected",
            remediation=(
                "Use --profile standard, or add [stages.audit] with the command appropriate "
                "for your stack after scaffolding."
            ),
        )
