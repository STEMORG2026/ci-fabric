"""Run context, command runner, project detection and scaffolding."""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path

import pytest

from ci_fabric.config import load_config
from ci_fabric.context import create_context
from ci_fabric.detect import detect_project, require_audit_command
from ci_fabric.errors import CiError, ConfigError
from ci_fabric.hooks import hook_status, install_hooks, uninstall_hooks
from ci_fabric.runner import exit_remediation, run_command
from ci_fabric.scaffold import scaffold
from tests.support import base_config, git, install_shim, make_repo, write_files


def fixture_context(tmp_path: Path, **kwargs: object):  # type: ignore[no-untyped-def]
    root = make_repo(tmp_path, **kwargs)  # type: ignore[arg-type]
    return root, create_context(root, load_config(root))


def test_placeholders_and_stage_paths(tmp_path: Path) -> None:
    root, context = fixture_context(tmp_path)
    (root / "sub").mkdir()
    assert context.render("{project}-{version}") == "fixture-1.0.0"
    assert context.render("{root}/x") == f"{root.resolve()}/x"
    assert context.render("${HOME}") == "$" + "{HOME}"
    with pytest.raises(ConfigError, match="unknown placeholder"):
        context.render("{nope}")
    stage = context.config.stage_or_default("unit")
    stage.cwd = "sub"
    assert context.stage_cwd(stage) == (root / "sub").resolve()
    stage.cwd = "../escape"
    with pytest.raises(ConfigError, match="escapes"):
        context.stage_cwd(stage)


def test_clean_environment_is_constructed_not_inherited(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    config = load_config(root)
    context = create_context(root, config, clean_env=True)
    assert context.clean_env is True
    assert context.base_env["CI"] == "1"
    assert "SOURCE_DATE_EPOCH" in context.base_env
    assert os.environ.get("HOME") == context.base_env.get("HOME")
    assert "PYTHONPATH" not in context.base_env


def test_clean_environment_preserves_ambient_oidc_for_keyless_signing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # cosign keyless signs with the ambient OIDC identity; under --clean-env
    # the request variables must survive or hosted releases cannot sign
    # (CF-07). They are ephemeral, scoped credentials, like SSH_AUTH_SOCK.
    root = make_repo(tmp_path)
    config = load_config(root)
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_URL", "https://token.example/request")
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "request-token")
    monkeypatch.setenv("SIGSTORE_ID_TOKEN", "id-token")
    context = create_context(root, config, clean_env=True)
    assert context.base_env["ACTIONS_ID_TOKEN_REQUEST_URL"] == "https://token.example/request"
    assert context.base_env["ACTIONS_ID_TOKEN_REQUEST_TOKEN"] == "request-token"
    assert context.base_env["SIGSTORE_ID_TOKEN"] == "id-token"


def test_source_date_epoch_from_commit(tmp_path: Path) -> None:
    _root, context = fixture_context(tmp_path)
    assert context.git.available is True
    assert context.git.tracked_files >= 5
    assert context.source_date_epoch > 1_500_000_000
    assert context.git.tags_at_head == []


def test_context_rejects_nested_repository_config(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    nested = root / "nested"
    nested.mkdir()
    (nested / "ci.toml").write_text((root / "ci.toml").read_text(), encoding="utf-8")
    (nested / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    config = load_config(nested)
    with pytest.raises(ConfigError, match="does not match"):
        create_context(root, config)


def test_no_git_requires_explicit_policy(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    (root / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    (root / "ci.toml").write_text(
        base_config().replace("allow_no_git = false", "allow_no_git = true"),
        encoding="utf-8",
    )
    context = create_context(root, load_config(root))
    assert context.git.available is False
    (root / "ci.toml").write_text(base_config(), encoding="utf-8")
    with pytest.raises(ConfigError, match="not a Git repository"):
        create_context(root, load_config(root))


def test_run_command_success_failure_and_log(tmp_path: Path) -> None:
    log = tmp_path / "logs" / "command.log"
    environment = dict(os.environ)
    result = run_command(
        "printf 'hello\\n'",
        cwd=tmp_path,
        env=environment,
        timeout=10,
        log_path=log,
    )
    assert result.ok is True
    assert result.exit_code == 0
    assert "hello" in result.output_tail
    assert "# command:" in log.read_text()

    failed = run_command(
        "printf 'boom\\n'; exit 7",
        cwd=tmp_path,
        env=environment,
        timeout=10,
        log_path=tmp_path / "failed.log",
    )
    assert failed.ok is False
    assert failed.exit_code == 7
    assert "boom" in failed.output_tail


def test_run_command_timeout_kills_process_group(tmp_path: Path) -> None:
    started = time.monotonic()
    result = run_command(
        "sleep 30",
        cwd=tmp_path,
        env=dict(os.environ),
        timeout=0.3,
        log_path=tmp_path / "timeout.log",
    )
    assert result.timed_out is True
    assert time.monotonic() - started < 10
    assert "timed out" in result.output_tail
    assert "Increase stages" in (exit_remediation(result) or "")


def test_run_command_missing_executable(tmp_path: Path) -> None:
    result = run_command(
        "definitely-not-a-real-command-ci-fabric",
        cwd=tmp_path,
        env=dict(os.environ),
        timeout=10,
        log_path=tmp_path / "missing.log",
    )
    assert result.ok is False
    assert result.exit_code == 127
    assert "not found" in (exit_remediation(result) or "")


def test_detect_python_project(tmp_path: Path) -> None:
    root = tmp_path / "python"
    write_files(
        root,
        {
            "pyproject.toml": (
                '[project]\nname = "demo"\nversion = "1.2.3"\n'
                '[build-system]\nrequires = ["setuptools"]\n'
                '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'
                "[tool.ruff]\nline-length = 90\n"
                "[tool.mypy]\nstrict = true\n"
                "[tool.coverage.report]\nfail_under = 81\n"
            ),
            "tests/test_demo.py": "def test_ok():\n    assert True\n",
            "requirements.txt": "requests==2.0.0\n",
        },
    )
    detection = detect_project(root)
    assert detection.name == "demo"
    assert detection.version == "1.2.3"
    assert detection.version_format == "semver"
    assert detection.commands["format"] == ["{python} -m ruff format --check ."]
    assert detection.commands["lint"] == ["{python} -m ruff check ."]
    assert detection.commands["typecheck"] == ["{python} -m mypy ."]
    assert "pytest" in detection.commands["unit"][0]
    assert detection.junit_path == "{artifacts}/junit-unit.xml"
    assert detection.coverage_report == "{artifacts}/coverage.xml"
    assert detection.coverage_min_percent == 81.0
    assert detection.audits == ["{python} -m pip_audit -r requirements.txt"]
    assert detection.build_outputs == ["dist"]


def test_detect_node_and_static_projects(tmp_path: Path) -> None:
    node = tmp_path / "node"
    write_files(
        node,
        {
            "package.json": (
                '{"name": "web", "version": "2.0.0", "scripts": {'
                '"build": "vite build", "lint": "eslint .", "test": "vitest run"},'
                '"devDependencies": {}}'
            ),
            "package-lock.json": "{}",
            "vite.config.ts": "export default {}\n",
        },
    )
    detection = detect_project(node)
    assert detection.name == "web"
    assert detection.commands["lint"] == ["npm run lint"]
    assert detection.commands["unit"] == ["npm test"]
    assert detection.commands["build"] == ["npm run build"]
    assert detection.package_inputs == ["dist"]
    assert detection.audits == ["npm audit --audit-level=high"]

    static = tmp_path / "static"
    write_files(static, {"index.html": "<html></html>", "css/site.css": "body{}"})
    detected = detect_project(static)
    assert detected.package_inputs == ["index.html", "css"]
    assert detected.package_must_contain == ["index.html"]


def test_require_audit_command_for_strict(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    write_files(root, {"README.md": "# x"})
    detection = detect_project(root)
    with pytest.raises(ConfigError, match="no supported lockfile"):
        require_audit_command(detection, "strict")
    require_audit_command(detection, "standard")


def test_scaffold_writes_loadable_config_and_shim(tmp_path: Path) -> None:
    root = tmp_path / "project"
    write_files(
        root,
        {
            "README.md": "# Demo\n",
            "LICENSE": "MIT\n",
            "pyproject.toml": (
                '[project]\nname = "demo"\nversion = "1.0.0"\n[tool.ruff]\nline-length = 100\n'
            ),
            "tests/test_one.py": "def test_one():\n    assert True\n",
        },
    )
    assert git(root, "init", "-b", "main").returncode == 0
    result = scaffold(root, profile="standard")
    assert result.config_path.is_file()
    assert result.shim_written is True
    assert (root / "ci").stat().st_mode & stat.S_IXUSR
    assert result.gitignore_updated is True
    text = result.config_path.read_text()
    assert 'profile = "standard"' in text
    assert "[stages.lint]" in text
    assert ".ci/" in (root / ".gitignore").read_text()
    config = load_config(root)
    assert config.project.name == "demo"
    with pytest.raises(ConfigError, match="already exists"):
        scaffold(root, profile="standard")
    again = scaffold(root, profile="minimal", force=True)
    assert 'profile = "minimal"' in again.config_path.read_text()


def test_scaffold_rejects_strict_without_audit(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    write_files(root, {"README.md": "# x"})
    with pytest.raises(ConfigError, match="no supported lockfile"):
        scaffold(root, profile="strict")


def test_hooks_chain_previous_hook_and_restore(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    install_shim(root)
    previous = root / "existing-hooks"
    previous.mkdir()
    marker = root / "previous-ran.txt"
    previous_hook = previous / "pre-commit"
    previous_hook.write_text(f"#!/bin/sh\necho ran >> {marker}\n", encoding="utf-8")
    previous_hook.chmod(0o755)
    assert git(root, "config", "--local", "core.hooksPath", str(previous)).returncode == 0

    state = install_hooks(root)
    assert state.installed is True
    assert state.previous_hooks_path == str(previous)
    dispatcher = root / ".localci" / "githooks" / "pre-commit"
    assert dispatcher.is_file()
    environment = dict(os.environ)
    environment["CI_FABRIC_SRC"] = str(Path(__file__).resolve().parents[2])
    completed = __import__("subprocess").run(
        [str(dispatcher)],
        cwd=root,
        text=True,
        capture_output=True,
        env=environment,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    assert marker.read_text().splitlines() == ["ran"]
    assert hook_status(root).installed is True

    restored = uninstall_hooks(root)
    assert restored.installed is False
    assert git(root, "config", "--local", "--get", "core.hooksPath").stdout.strip() == str(previous)


def test_hooks_chain_and_restore_foreign_path(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    foreign = root / "foreign"
    foreign.mkdir()
    assert git(root, "config", "--local", "core.hooksPath", str(foreign)).returncode == 0
    state = install_hooks(root)
    assert state.installed is True
    assert state.previous_hooks_path == str(foreign)
    state = uninstall_hooks(root, force=True)
    assert state.installed is False
    assert git(root, "config", "--local", "--get", "core.hooksPath").stdout.strip() == str(foreign)


def test_hook_run_rejects_unknown_hook(tmp_path: Path) -> None:
    _root, context = fixture_context(tmp_path)
    from ci_fabric.hooks import run_hook

    with pytest.raises(CiError, match="unsupported hook"):
        run_hook(context, "post-merge")


def test_git_helpers_report_repository_state(tmp_path: Path) -> None:
    _root, context = fixture_context(tmp_path)
    assert context.git.branch == "main"
    assert len(context.git.short_revision) == 12
    (Path(context.root) / "dirty.txt").write_text("dirty")
    fresh = create_context(context.root, context.config)
    assert fresh.git.dirty is True
    assert "dirty.txt" in fresh.git.dirty_files
