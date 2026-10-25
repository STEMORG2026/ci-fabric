"""Local secret scanning.

This is a deliberately small, dependency-free scanner. It detects common
credential shapes and high-entropy assignments in files that are part of the
build input (tracked files by default). It is not a replacement for a
history-aware scanner such as gitleaks or TruffleHog; that gap is documented.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .util import file_size, glob_match, looks_binary, read_text, tracked_files


@dataclass(frozen=True, slots=True)
class SecretRule:
    id: str
    description: str
    pattern: re.Pattern[str]
    entropy_check: bool = False


@dataclass(slots=True)
class Finding:
    path: str
    line: int
    rule: str
    description: str
    context: str
    fingerprint: str

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "line": self.line,
            "rule": self.rule,
            "description": self.description,
            "context": self.context,
            "fingerprint": self.fingerprint,
        }


@dataclass(slots=True)
class ScanReport:
    files_scanned: int = 0
    files_skipped: int = 0
    findings: list[Finding] = field(default_factory=list)
    allowed: list[Finding] = field(default_factory=list)
    skipped_large: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.findings

    def to_dict(self) -> dict[str, object]:
        return {
            "files_scanned": self.files_scanned,
            "files_skipped": self.files_skipped,
            "findings": [finding.to_dict() for finding in self.findings],
            "allowed": [finding.to_dict() for finding in self.allowed],
            "skipped_large": self.skipped_large,
        }


_RULES: tuple[SecretRule, ...] = (
    SecretRule(
        "aws-access-key",
        "AWS access key ID",
        re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ),
    SecretRule(
        "private-key",
        "PEM private key block",
        re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----"),
    ),
    SecretRule(
        "github-token",
        "GitHub token",
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b"),
    ),
    SecretRule(
        "gitlab-token",
        "GitLab token",
        re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"),
    ),
    SecretRule(
        "slack-token",
        "Slack token",
        re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    ),
    SecretRule(
        "stripe-key",
        "Stripe secret key",
        re.compile(r"\b(?:sk|rk)_(?:live|test)_[0-9A-Za-z]{16,}\b"),
    ),
    SecretRule(
        "google-api-key",
        "Google API key",
        re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    ),
    SecretRule(
        "npm-token",
        "npm token",
        re.compile(r"\bnpm_[A-Za-z0-9]{36}\b"),
    ),
    SecretRule(
        "pypi-token",
        "PyPI token",
        re.compile(r"\bpypi-[A-Za-z0-9_-]{20,}\b"),
    ),
    SecretRule(
        "jwt",
        "JSON Web Token",
        re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    ),
    SecretRule(
        "generic-assignment",
        "high-entropy value assigned to a secret-like name",
        re.compile(
            r"(?i)(?:api[_-]?key|secret|token|password|passwd|client[_-]?secret|"
            r"private[_-]?key|auth[_-]?key)\s*[:=]\s*[\"']?"
            r"([A-Za-z0-9_./+=~-]{16,200})[\"']?"
        ),
        entropy_check=True,
    ),
)

_ALLOW_MARKER = re.compile(r"(?i)(localci:allow-secret|pragma:\s*allowlist\s+secret)")
_PLACEHOLDER = re.compile(
    r"(?i)(example|sample|dummy|fake|placeholder|changeme|replace[_-]?me|"
    r"redacted|notsecret|your[_-]|xxx+|\*\*\*|<[^>]+>|\$\{|@[a-z-]+@)"
)
_TOKEN_IN_LINE = re.compile(r"[A-Za-z0-9_./+=~-]{16,}")


def _entropy(value: str) -> float:
    if not value:
        return 0.0
    counts: dict[str, int] = {}
    for character in value:
        counts[character] = counts.get(character, 0) + 1
    total = len(value)
    return -sum((count / total) * math.log2(count / total) for count in counts.values())


def _looks_placeholder(value: str) -> bool:
    if _PLACEHOLDER.search(value):
        return True
    stripped = value.strip("\"'")
    if not stripped or stripped.startswith(("/", "~", "$", "%", "{")):
        return True
    return len(set(stripped)) <= 4


def _redact_line(line: str) -> str:
    redacted = _TOKEN_IN_LINE.sub(lambda match: match.group(0)[:4] + "...", line.strip())
    return redacted[:180]


def _allowlisted(
    finding: Finding,
    *,
    path_relative: str,
    line_text: str,
    previous_line: str,
    allowlist: Sequence[str],
) -> bool:
    if _ALLOW_MARKER.search(line_text) or _ALLOW_MARKER.search(previous_line):
        return True
    for entry in allowlist:
        entry = entry.strip()
        if not entry:
            continue
        if entry == finding.fingerprint:
            return True
        if ":" in entry:
            pattern, _, rule = entry.partition(":")
            if rule and rule != finding.rule:
                continue
            if glob_match(path_relative, pattern) or path_relative.startswith(
                pattern.rstrip("/") + "/"
            ):
                return True
        elif glob_match(path_relative, entry):
            return True
    return False


def _scan_text(path: Path, relative: str, allowlist: Sequence[str], report: ScanReport) -> None:
    try:
        text = read_text(path)
    except Exception:
        report.files_skipped += 1
        return
    lines = text.splitlines()
    for index, line in enumerate(lines, start=1):
        if len(line) > 20_000:
            line = line[:20_000]
        previous = lines[index - 2] if index >= 2 else ""
        for rule in _RULES:
            for match in rule.pattern.finditer(line):
                value = match.group(1) if rule.entropy_check and match.groups() else match.group(0)
                if rule.entropy_check:
                    if _looks_placeholder(value):
                        continue
                    if _entropy(value) < 3.1 or len(set(value)) < 8:
                        continue
                fingerprint = (
                    __import__("hashlib")
                    .sha256(f"{rule.id}:{relative}:{index}:{value}".encode())
                    .hexdigest()
                )
                finding = Finding(
                    path=relative,
                    line=index,
                    rule=rule.id,
                    description=rule.description,
                    context=_redact_line(line),
                    fingerprint=fingerprint,
                )
                if _allowlisted(
                    finding,
                    path_relative=relative,
                    line_text=line,
                    previous_line=previous,
                    allowlist=allowlist,
                ):
                    report.allowed.append(finding)
                else:
                    report.findings.append(finding)


def scan_files(
    root: Path,
    paths: Iterable[Path],
    *,
    allowlist: Sequence[str] = (),
    max_bytes: int = 5 * 1024 * 1024,
) -> ScanReport:
    report = ScanReport()
    seen: set[Path] = set()
    for candidate in sorted(paths, key=lambda item: item.as_posix()):
        try:
            resolved = candidate.resolve()
            relative = resolved.relative_to(root.resolve()).as_posix()
        except (OSError, ValueError):
            continue
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        if file_size(resolved) > max_bytes:
            report.skipped_large.append(relative)
            continue
        if looks_binary(resolved):
            report.files_skipped += 1
            continue
        report.files_scanned += 1
        _scan_text(resolved, relative, allowlist, report)
    return report


def scan_repository(
    root: Path,
    *,
    include_untracked: bool,
    allowlist: Sequence[str],
    exclude_globs: Sequence[str] = (),
) -> ScanReport:
    tracked = tracked_files(root)
    files = [path for relative in tracked if (path := root / relative).is_file()]
    if include_untracked:
        from .util import iter_files

        excluded = {
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
        files.extend(iter_files(root, excluded_dirs=excluded, excluded_globs=exclude_globs))
    return scan_files(root, files, allowlist=allowlist)


def scan_directory(
    root: Path,
    directory: Path,
    *,
    allowlist: Sequence[str],
    max_files: int = 20_000,
) -> ScanReport:
    from .util import iter_files

    files: list[Path] = []
    for index, path in enumerate(iter_files(directory, excluded_dirs={".git", "node_modules"})):
        if index >= max_files:
            break
        files.append(path)
    report = scan_files(directory, files, allowlist=allowlist)
    # Report paths relative to the original repository root when possible.
    for finding in report.findings + report.allowed:
        finding.path = f"{root.name}/{finding.path}"
    return report


def deploy_config_findings(config: Config) -> list[str]:
    findings: list[str] = []
    for name, env in config.deploy.items():
        for key, value in env.env.items():
            if re.search(
                r"(?i)(secret|token|password|passwd|key|credential)", key
            ) and not re.match(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$", value):
                findings.append(f"deploy.{name}.env.{key} is a literal secret-like value")
    return findings
