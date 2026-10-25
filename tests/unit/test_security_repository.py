"""Secret scanner and repository integrity checks."""

from __future__ import annotations

from pathlib import Path

from ci_fabric.repository import (
    check_lockfiles,
    check_markdown_links,
    check_required_paths,
    check_tracked_files,
    check_versions,
)
from ci_fabric.security import scan_files, scan_repository
from tests.support import BASE_FILES, commit_all, git, make_repo, write_files

AWS_KEY = "AKIA" + "ABCDEFGHIJKLMNOP"
TOKEN = "ghp_" + "A" * 36


def test_secret_scanner_finds_and_redacts_credentials(tmp_path: Path) -> None:
    root = make_repo(tmp_path, files={"app.py": f"aws_key = '{AWS_KEY}'\n"})
    report = scan_repository(root, include_untracked=False, allowlist=[])
    assert report.files_scanned >= 1
    assert [finding.rule for finding in report.findings] == ["aws-access-key"]
    assert AWS_KEY not in report.findings[0].context
    assert "AKIA..." in report.findings[0].context


def test_secret_scanner_supports_allowlist_by_path_and_rule(tmp_path: Path) -> None:
    root = make_repo(tmp_path, files={"tests/fixtures/leak.py": f"TOKEN = '{TOKEN}'\n"})
    report = scan_repository(
        root,
        include_untracked=False,
        allowlist=["tests/fixtures/*.py:github-token"],
    )
    assert not report.findings
    assert len(report.allowed) == 1


def test_secret_scanner_marker_and_placeholders(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        files={
            "allowed.py": f"TOKEN = '{TOKEN}'  # localci:allow-secret\n",
            "placeholder.py": 'password = "your-password-here"\n',
            "obvious.py": 'password = "aaaaaaaaaaaaaaaa"\n',
        },
    )
    report = scan_repository(root, include_untracked=False, allowlist=[])
    assert not report.findings
    assert len(report.allowed) == 1


def test_secret_scanner_ignores_binary_and_large_files(tmp_path: Path) -> None:
    (tmp_path / "bin.dat").write_bytes(b"\x00" + TOKEN.encode())
    (tmp_path / "huge.txt").write_text(TOKEN + "\n" + "x" * 200, encoding="utf-8")
    report = scan_files(
        tmp_path,
        [tmp_path / "bin.dat", tmp_path / "huge.txt"],
        allowlist=[],
        max_bytes=100,
    )
    assert report.files_scanned == 0
    assert report.files_skipped == 1
    assert report.skipped_large == ["huge.txt"]


def test_scan_repository_untracked_switch(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    write_files(root, {"untracked.py": f"TOKEN = '{TOKEN}'\n"})
    tracked_only = scan_repository(root, include_untracked=False, allowlist=[])
    assert not tracked_only.findings
    everything = scan_repository(root, include_untracked=True, allowlist=[])
    assert len(everything.findings) == 1


def test_check_versions_across_formats(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        files={
            "VERSION": "1.2.3\n",
            "package.json": '{"version": "1.2.3"}\n',
            "pyproject.toml": '[project]\nname = "x"\nversion = "1.2.3"\n',
            "app.py": 'VERSION = "1.2.3"\n',
        },
        config=(
            'version = 1\n[project]\nname = "x"\nversion = "1.2.3"\n'
            '[policy]\nprofile = "minimal"\n'
            "[repository]\n"
            '[[repository.versions]]\npath = "VERSION"\nformat = "text"\n'
            '[[repository.versions]]\npath = "package.json"\nformat = "json"\nkey = "version"\n'
            '[[repository.versions]]\npath = "pyproject.toml"\nformat = "toml"\n'
            'key = "project.version"\n'
            '[[repository.versions]]\npath = "app.py"\nformat = "regex"\n'
            'regex = \'VERSION = "([^"]+)"\'\n'
            "[stages.security]\nsecret_scan = true\nscan_config = false\n"
        ),
    )
    from ci_fabric.config import load_config

    config = load_config(root)
    assert check_versions(config) == []


def test_check_versions_reports_mismatch(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        files={"package.json": '{"version": "9.9.9"}\n'},
        config=(
            'version = 1\n[project]\nname = "x"\nversion = "1.2.3"\n'
            '[policy]\nprofile = "minimal"\n'
            "[repository]\n"
            '[[repository.versions]]\npath = "package.json"\nformat = "json"\nkey = "version"\n'
            "[stages.security]\nsecret_scan = true\nscan_config = false\n"
        ),
    )
    from ci_fabric.config import load_config

    problems = check_versions(load_config(root))
    assert len(problems) == 1
    assert problems[0].actual == "9.9.9"
    assert "mismatch" in problems[0].reason


def test_markdown_link_checking(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        files={
            "README.md": (
                "# Title\n"
                "[ok](LICENSE)\n"
                "[web](https://example.invalid)\n"
                "[anchor](#title)\n"
                "[bad](missing.md)\n"
            ),
            "nested/guide.md": "[up](../README.md)\n[bad](../nope.md)\n",
        },
    )
    tracked = ["README.md", "nested/guide.md"]
    checked, problems = check_markdown_links(root, tracked, globs=["**/*.md"], ignore=[])
    assert checked == 2
    assert {problem.target for problem in problems} == {"missing.md", "../nope.md"}


def test_tracked_file_policy(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    write_files(
        root,
        {
            ".env": "SECRET=x\n",
            "big.bin": "x" * 2048,
            "conflict.txt": "before\n<<<<<<< HEAD\nafter\n",
        },
    )
    commit_all(root, "problem files")
    report = check_tracked_files(
        root,
        [".env", "big.bin", "conflict.txt", "README.md"],
        forbidden_patterns=[".env"],
        max_file_bytes=1024,
        conflict_markers=True,
    )
    assert report["forbidden"] == [".env"]
    assert report["conflicts"] == ["conflict.txt:2"]
    assert report["large"] == [("big.bin", 2048)]


def test_lockfile_and_required_path_checks(tmp_path: Path) -> None:
    root = make_repo(tmp_path, files={"requirements.lock": "x\n"})
    write_files(root, {"untracked.lock": "y\n"})
    assert check_required_paths(root, ["README.md", "missing.txt"]) == ["missing.txt"]
    result = check_lockfiles(
        root,
        ["requirements.lock", "untracked.lock", "absent.lock"],
        ["README.md", "requirements.lock", "ci.toml", "VERSION"],
    )
    assert result["missing"] == ["absent.lock"]
    assert result["untracked"] == ["untracked.lock"]


def test_git_helper_detects_ignored_state(tmp_path: Path) -> None:
    root = make_repo(tmp_path, files={".gitignore": ".ci/\n.localci/\n"})
    assert git(root, "check-ignore", "-q", ".ci/").returncode == 0
    assert BASE_FILES[".gitignore"] == ".ci/\n.localci/\n"
