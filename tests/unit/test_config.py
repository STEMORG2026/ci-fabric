"""Strict configuration parsing: every typo must be a hard error."""

from __future__ import annotations

from pathlib import Path

import pytest

from ci_fabric.config import (
    STAGE_NAMES,
    Config,
    load_config,
    validate_pep440,
    validate_semver,
)
from ci_fabric.errors import ConfigError

BASE = """\
version = 1

[project]
name = "fixture"
version = "1.2.3"
version_format = "semver"

[policy]
profile = "minimal"
fail_fast = true

[stages.security]
secret_scan = true
scan_config = true
"""


def write_config(root: Path, text: str = BASE) -> Path:
    (root / "ci.toml").write_text(text, encoding="utf-8")
    return root


def load(tmp_path: Path, text: str = BASE) -> Config:
    return load_config(write_config(tmp_path, text))


def test_loads_minimal_configuration(tmp_path: Path) -> None:
    config = load(tmp_path)
    assert config.project.name == "fixture"
    assert config.project.version == "1.2.3"
    assert config.policy.profile == "minimal"
    assert set(config.effective_required_stages()) == {"repository", "security"}
    assert config.stage("security") is not None
    assert config.stage("unit") is None
    assert config.policy_document()["stages"]["unit"]["configured"] is False


def test_unknown_top_level_key_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="unknown key"):
        load(tmp_path, BASE + '\n[deploy.prod]\nstrategy = "process"\nstar_command = "x"\n')


def test_unknown_stage_key_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="unknown key"):
        load(tmp_path, BASE + '\n[stages.lint]\ncomand = "x"\n')


def test_unknown_stage_table_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="unknown stage table"):
        load(tmp_path, BASE + '\n[stages.deploy]\ncommand = "x"\n')


def test_schema_version_is_mandatory(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="version must be 1"):
        load(tmp_path, BASE.replace("version = 1", "version = 2"))


def test_missing_configuration_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path)


def test_profile_requires_audit_stage(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="audit"):
        load(tmp_path, BASE.replace('profile = "minimal"', 'profile = "strict"'))


def test_required_stage_must_have_a_command(tmp_path: Path) -> None:
    text = BASE.replace('profile = "minimal"', 'profile = "minimal"\nrequired_stages = ["unit"]')
    with pytest.raises(ConfigError, match="not configured"):
        load(tmp_path, text)


def test_disabled_required_stage_is_rejected(tmp_path: Path) -> None:
    text = BASE.replace(
        "[stages.security]",
        '[stages.security]\nenabled = false\ncommand = "true"',
    )
    with pytest.raises(ConfigError, match="disabled"):
        load(tmp_path, text)


def test_command_and_commands_are_mutually_exclusive(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="either command or commands"):
        load(
            tmp_path,
            BASE + '\n[stages.lint]\ncommand = "a"\ncommands = ["b"]\n',
        )


def test_stage_specific_key_is_rejected_elsewhere(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="unknown key"):
        load(tmp_path, BASE + "\n[stages.lint]\nmin_percent = 80\n")


def test_coverage_threshold_accepts_tier_measured_coverage(tmp_path: Path) -> None:
    config = load(
        tmp_path,
        BASE
        + '\n[stages.unit]\ncommand = "python -m pytest"\nmeasure_coverage = true\n'
        + "\n[stages.coverage]\nmin_percent = 80\n",
    )
    unit = config.stage("unit")
    coverage = config.stage("coverage")
    assert unit is not None and coverage is not None
    assert unit.measure_coverage is True
    assert coverage.min_percent == 80


def test_disabled_stage_must_state_its_reason(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="does not say why"):
        load(tmp_path, BASE + "\n[stages.lint]\nenabled = false\n")


def test_disabled_stage_records_the_reason(tmp_path: Path) -> None:
    config = load(
        tmp_path,
        BASE
        + "\n[stages.lint]\nenabled = false\n"
        + 'disabled_reason = "owner: sajan - linter not ported yet"\n',
    )
    lint = config.stage("lint")
    assert lint is not None
    assert lint.enabled is False
    assert lint.disabled_reason == "owner: sajan - linter not ported yet"


def test_disabled_reason_without_a_disabled_stage_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="no effect while the stage is enabled"):
        load(tmp_path, BASE + '\n[stages.lint]\ndisabled_reason = "why"\n')


def test_coverage_threshold_without_any_source_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="no coverage source"):
        load(tmp_path, BASE + "\n[stages.coverage]\nmin_percent = 80\n")


def test_measure_coverage_is_rejected_on_a_stage_that_is_not_a_test_tier(
    tmp_path: Path,
) -> None:
    with pytest.raises(ConfigError, match="unknown key"):
        load(tmp_path, BASE + '\n[stages.lint]\ncommand = "x"\nmeasure_coverage = true\n')


def test_stage_parsing_and_defaults(tmp_path: Path) -> None:
    config = load(
        tmp_path,
        BASE
        + """
[stages.unit]
command = "python -m pytest"
required = true
timeout = 120
cwd = "sub"
test_globs = ["tests/**/*.py"]
min_tests = 3
junit_path = "{artifacts}/unit.xml"
env = { PYTHONHASHSEED = "0" }

[stages.package]
strategy = "archive"
format = "zip"
inputs = ["dist"]
output = "{artifacts}/out.zip"
exclude = ["*.pyc"]
""",
    )
    unit = config.stage("unit")
    assert unit is not None
    assert unit.required is True
    assert unit.timeout_seconds == 120
    assert unit.cwd == "sub"
    assert unit.commands == ["python -m pytest"]
    assert unit.env == {"PYTHONHASHSEED": "0"}
    package = config.stage("package")
    assert package is not None
    assert package.archive_format == "zip"
    assert package.strategy == "archive"


def test_version_sources_are_parsed(tmp_path: Path) -> None:
    config = load(
        tmp_path,
        BASE
        + """
[[repository.versions]]
path = "VERSION"
format = "text"

[[repository.versions]]
path = "package.json"
format = "json"
key = "version"
""",
    )
    assert [(source.path, source.format) for source in config.repository.versions] == [
        ("VERSION", "text"),
        ("package.json", "json"),
    ]


def test_version_mismatch_between_literal_and_file(tmp_path: Path) -> None:
    (tmp_path / "VERSION").write_text("2.0.0\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="does not match"):
        load(
            tmp_path,
            BASE.replace('version = "1.2.3"', 'version = "1.2.3"\nversion_file = "VERSION"'),
        )


def test_version_file_is_read_and_validated(tmp_path: Path) -> None:
    (tmp_path / "VERSION").write_text("not-a-version\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid semver"):
        load(
            tmp_path,
            BASE.replace('version = "1.2.3"', 'version_file = "VERSION"'),
        )
    (tmp_path / "VERSION").write_text("2.1.0\n", encoding="utf-8")
    config = load(tmp_path, BASE.replace('version = "1.2.3"', 'version_file = "VERSION"'))
    assert config.project.version == "2.1.0"


@pytest.mark.parametrize("version", ["1.0", "v1.0.0", "1.0.0.0", "01.2.3"])
def test_invalid_semver(version: str) -> None:
    assert validate_semver(version) is False


@pytest.mark.parametrize("version", ["1.0.0", "1.0.0-rc.1", "1.0.0+build.5"])
def test_valid_semver(version: str) -> None:
    assert validate_semver(version) is True


@pytest.mark.parametrize("version", ["1.0", "1.0.0.post1", "2.0rc1", "1.0.dev2"])
def test_valid_pep440(version: str) -> None:
    assert validate_pep440(version) is True


def test_invalid_policy_values(tmp_path: Path) -> None:
    for replacement in (
        'profile = "bogus"',
        'require_clean_worktree = "sometimes"',
        "keep_runs = 0",
        'required_stages = ["nope"]',
        'fail_fast = "yes"',
    ):
        text = BASE.replace("fail_fast = true", replacement)
        with pytest.raises(ConfigError):
            load(tmp_path, text)


@pytest.mark.parametrize("value", [".env", ".env.local", "*.pem", "node_modules/**"])
def test_forbidden_paths_defaults_cover_common_secrets(tmp_path: Path, value: str) -> None:
    import fnmatch

    config = load(tmp_path)
    assert any(fnmatch.fnmatch(value, pattern) for pattern in config.repository.forbidden_paths)


def test_deploy_process_requires_start_command(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="start_command is required"):
        load(tmp_path, BASE + '\n[deploy.local]\nstrategy = "process"\n')


def test_deploy_static_defaults_and_secret_literal_rejection(tmp_path: Path) -> None:
    config = load(
        tmp_path,
        BASE
        + """
[deploy.local]
strategy = "static"
port = 0
env = { PUBLIC_BASE_URL = "https://example.invalid" }
""",
    )
    env = config.deploy["local"]
    assert env.health_path == "/"
    assert env.auto_rollback is True
    with pytest.raises(ConfigError, match="looks secret"):
        load(
            tmp_path,
            BASE
            + '\n[deploy.local]\nstrategy = "static"\nport = 0\nenv = { API_TOKEN = "literal" }\n',
        )


def test_env_expansion_never_silently_empties(tmp_path: Path) -> None:
    from ci_fabric.config import expand_env_value

    assert expand_env_value("x-${NAME}", {"NAME": "value"}) == "x-value"
    with pytest.raises(ConfigError, match="not set"):
        expand_env_value("x-${MISSING}", {})


def test_command_program_skips_env_assignments(tmp_path: Path) -> None:
    from ci_fabric.config import command_program

    assert command_program("PYTHONHASHSEED=0 python -m pytest") == "python"
    assert command_program('"unterminated') is None


def test_relative_paths_cannot_escape(tmp_path: Path) -> None:
    text = BASE.replace('version = "1.2.3"', 'version_file = "../VERSION"')
    with pytest.raises(ConfigError, match="relative path"):
        load(tmp_path, text)


def test_every_stage_name_is_parseable(tmp_path: Path) -> None:
    for name in STAGE_NAMES:
        if name == "security":
            continue
        section = f"\n[stages.{name}]\nrequired = false\n"
        config = load(tmp_path, BASE + section)
        assert config.stage(name) is not None
