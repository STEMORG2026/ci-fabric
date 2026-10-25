"""Test fixtures and helpers shared across the ci-fabric test suite."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable

BASE_FILES: dict[str, str] = {
    "README.md": "# Fixture project\n",
    "LICENSE": "MIT License\n\nCopyright (c) fixture\n",
    ".gitignore": ".ci/\n.localci/\n",
}


def base_config(
    *,
    version: str = "1.0.0",
    profile: str = "minimal",
    policy: str = "",
    sections: Sequence[str] | str = (),
    project: str = "",
    release: str = "",
) -> str:
    # A single section string is accepted as a convenience; without this a
    # parenthesized (non-tuple) string would be iterated character by character.
    section_list: Sequence[str] = (sections,) if isinstance(sections, str) else sections
    # Extra policy lines override the defaults by key instead of duplicating them
    # (duplicate TOML keys are a hard parse error).
    extra_policy = [line.strip() for line in policy.splitlines() if line.strip()]
    overridden = {line.split("=", 1)[0].strip() for line in extra_policy}
    policy_lines = [
        f'profile = "{profile}"',
        "fail_fast = true",
        "keep_runs = 20",
        "allow_no_git = false",
        "require_test_discovery = true",
    ]
    policy_lines = [
        line for line in policy_lines if line.split("=", 1)[0].strip() not in overridden
    ]
    policy_lines.extend(extra_policy)
    parts = [
        "version = 1",
        "",
        "[project]",
        'name = "fixture"',
        'version_file = "VERSION"',
        'version_format = "semver"',
    ]
    if project:
        parts.append(project)
    parts.extend(
        [
            "",
            "[policy]",
            *policy_lines,
            "",
            "[stages.security]",
            "required = true",
            "secret_scan = true",
            "scan_config = true",
        ]
    )
    for section in section_list:
        parts.extend(["", section.strip("\n")])
    if release:
        parts.extend(["", "[release]", release.strip("\n")])
    parts.extend(["", ""])
    return "\n".join(parts)


def git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
        env=_git_env(),
    )


def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "CI Fixture",
            "GIT_AUTHOR_EMAIL": "ci@example.invalid",
            "GIT_COMMITTER_NAME": "CI Fixture",
            "GIT_COMMITTER_EMAIL": "ci@example.invalid",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return env


def commit_all(root: Path, message: str = "fixture") -> str:
    git(root, "add", "-A")
    result = git(root, "commit", "-m", message)
    assert result.returncode == 0, result.stderr
    return git(root, "rev-parse", "HEAD").stdout.strip()


def write_files(root: Path, files: Mapping[str, str]) -> None:
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")


def make_repo(
    tmp_path: Path,
    *,
    files: Mapping[str, str] | None = None,
    config: str | None = None,
    version: str = "1.0.0",
    profile: str = "minimal",
    policy: str = "",
    sections: Sequence[str] | str = (),
    project: str = "",
    release: str = "",
    commit: bool = True,
    git_init: bool = True,
    name: str = "repo",
) -> Path:
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    all_files = dict(BASE_FILES)
    all_files["VERSION"] = version + "\n"
    if files:
        all_files.update(files)
    write_files(root, all_files)
    (root / "ci.toml").write_text(
        config
        if config is not None
        else base_config(
            version=version,
            profile=profile,
            policy=policy,
            sections=sections,
            project=project,
            release=release,
        ),
        encoding="utf-8",
        newline="\n",
    )
    if git_init:
        assert git(root, "init", "-b", "main").returncode == 0
        git(root, "config", "user.name", "CI Fixture")
        git(root, "config", "user.email", "ci@example.invalid")
        if commit:
            commit_all(root, "initial fixture")
    return root


def bump_version(root: Path, version: str, *, commit: bool = True) -> None:
    (root / "VERSION").write_text(version + "\n", encoding="utf-8", newline="\n")
    if commit:
        commit_all(root, f"release {version}")


def cli_env(**overrides: str | None) -> dict[str, str]:
    env = dict(os.environ)
    env.pop("CI_FABRIC_SRC", None)
    env.pop("PYTHONPATH", None)
    env["NO_COLOR"] = "1"
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def run_cli(
    root: Path,
    *args: str,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [PYTHON, "-m", "ci_fabric", "--root", str(root), *args]
    environment = dict(env or cli_env())
    # When the coverage stage sets CI_FABRIC_COVERAGE=1, CLI subprocesses are
    # measured deliberately (they exit cleanly, unlike deploy servers). This
    # keeps the CLI's gate coverage honest without blanket subprocess patching.
    if environment.get("CI_FABRIC_COVERAGE") == "1":
        # --branch keeps child data compatible with the parent's branch data;
        # children run with cwd in the fixture repo, where no coverage config
        # exists, so branch measurement must be requested explicitly.
        command = [
            PYTHON,
            "-m",
            "coverage",
            "run",
            "--parallel-mode",
            "--branch",
            "-m",
            "ci_fabric",
            "--root",
            str(root),
            *args,
        ]
    return subprocess.run(
        command,
        cwd=str(cwd or root),
        text=True,
        capture_output=True,
        check=False,
        env=environment,
        timeout=300,
    )


def run_fixture_pipeline(
    root: Path,
    *,
    command: str = "ci",
    stages: Sequence[str] | None = None,
    release: Any = None,
    fix: bool = False,
    finalize_result: bool = True,
) -> tuple[Any, Any]:
    from ci_fabric.config import load_config
    from ci_fabric.context import create_context
    from ci_fabric.pipeline import run_pipeline
    from ci_fabric.report import finalize

    config = load_config(root)
    context = create_context(root, config)
    result = run_pipeline(
        context,
        command=command,
        stages=stages,
        fix=fix,
        release=release,
        echo=lambda _message: None,
    )
    if finalize_result:
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
            echo=lambda _message: None,
        )
    return result, context


def latest_result(root: Path) -> dict[str, Any]:
    path = root / ".ci" / "results" / "latest.json"
    return json.loads(path.read_text(encoding="utf-8"))


def stage(result: Any, name: str) -> Any:
    for item in result.stages:
        if item.name == name:
            return item
    raise AssertionError(f"stage not found: {name}; have {[item.name for item in result.stages]}")


def gate(result: Any, gate_id: str) -> Any:
    for item in result.all_gates:
        if item.id == gate_id:
            return item
    raise AssertionError(f"gate not found: {gate_id}")


def install_shim(root: Path) -> Path:
    from ci_fabric.scaffold import SHIM

    path = root / "ci"
    path.write_text(SHIM, encoding="utf-8", newline="\n")
    path.chmod(0o755)
    return path


def copy_package_source(tmp_path: Path) -> Path:
    target = tmp_path / "ci-fabric-src"
    shutil.copytree(
        PROJECT_ROOT / "src" / "ci_fabric",
        target / "src" / "ci_fabric",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    return target


def static_deploy_sections(*, body: str = "Fixture v1", auto_rollback: bool = True) -> list[str]:
    del body
    return [
        '[stages.package]\nrequired = true\nstrategy = "archive"\n'
        'inputs = ["app"]\nstrip_prefix = "app"\noutput = "{artifacts}/package.tar.gz"',
        "[stages.artifact]\nrequired = true\nverify_archives = true\n"
        'scan_secrets = true\nmust_contain = ["index.html"]',
        "[deploy.local]\n"
        'strategy = "static"\n'
        "port = 0\n"
        'health_path = "/"\n'
        'smoke_path = "/"\n'
        f"auto_rollback = {'true' if auto_rollback else 'false'}\n"
        "stop_timeout = 5\n"
        "health_timeout = 15",
    ]


def write_static_app(root: Path, body: str, *, directory: str = "app") -> None:
    write_files(root, {f"{directory}/index.html": f"<html><body>{body}</body></html>\n"})


PROCESS_SERVER = """\
import http.server
import os
import sys

FAIL = {fail}
VERSION = {version!r}

if FAIL:
    print("intentional startup failure", flush=True)
    sys.exit(2)

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/health", "/ready"):
            body = b"ok"
            self.send_response(200)
        elif self.path == "/version":
            body = VERSION.encode()
            self.send_response(200)
        else:
            body = b"not found"
            self.send_response(404)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass

host = os.environ.get("HOST", "127.0.0.1")
port = int(os.environ["PORT"])
http.server.HTTPServer((host, port), Handler).serve_forever()
"""


def process_deploy_sections(*, smoke_expect: str, auto_rollback: bool = True) -> list[str]:
    return [
        '[stages.package]\nrequired = true\nstrategy = "archive"\n'
        'inputs = ["app"]\nstrip_prefix = "app"\noutput = "{artifacts}/package.tar.gz"',
        "[stages.artifact]\nrequired = true\nverify_archives = true\n"
        'scan_secrets = true\nmust_contain = ["server.py"]',
        "[deploy.local]\n"
        'strategy = "process"\n'
        "port = 0\n"
        'health_path = "/health"\n'
        'readiness_path = "/ready"\n'
        'health_expect = "ok"\n'
        'smoke_path = "/version"\n'
        f'smoke_expect = "{smoke_expect}"\n'
        'start_command = "{python} server.py"\n'
        f"auto_rollback = {'true' if auto_rollback else 'false'}\n"
        "stop_timeout = 5\n"
        "health_timeout = 15\n"
        "smoke_timeout = 30",
    ]


def process_server_source(*, fail: bool = False, version: str = "v1.0.0") -> str:
    return PROCESS_SERVER.format(fail=fail, version=version)


def free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
