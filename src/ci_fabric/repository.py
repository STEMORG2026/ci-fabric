"""Pure repository-integrity checks used by the repository stage."""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

from .config import Config, VersionSource
from .errors import CiError
from .util import file_size, looks_binary, matches_any, read_text


@dataclass(slots=True)
class LinkProblem:
    source: str
    line: int
    target: str
    reason: str


@dataclass(slots=True)
class VersionProblem:
    source: str
    expected: str | None
    actual: str | None
    reason: str


_FENCE = re.compile(r"^\s*(```|~~~)")
_INLINE_LINK = re.compile(r"!?\[[^\]]*]\(([^)\s]+)(?:\s+[\"'][^\"']*[\"'])?\)")
_REFERENCE_LINK = re.compile(r"^\s*\[[^\]]+]:\s+(\S+)")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_CONFLICT_MARKERS = ("<<<<<<< ", ">>>>>>> ", "||||||| ")


def _matches(path: str, patterns: list[str]) -> bool:
    return matches_any(path, patterns)


def _strip_fences(text: str) -> str:
    output: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            output.append("")
            continue
        output.append("" if in_fence else line)
    return "\n".join(output)


def check_markdown_links(
    root: Path,
    tracked: list[str],
    *,
    globs: list[str],
    ignore: list[str],
) -> tuple[int, list[LinkProblem]]:
    checked = 0
    problems: list[LinkProblem] = []
    for relative in tracked:
        if not relative.lower().endswith(".md"):
            continue
        if not _matches(relative, globs) or _matches(relative, ignore):
            continue
        path = root / relative
        try:
            text = read_text(path, limit=2 * 1024 * 1024)
        except CiError:
            continue
        text = _strip_fences(text)
        checked += 1
        for line_number, line in enumerate(text.splitlines(), start=1):
            targets = [match.group(1) for match in _INLINE_LINK.finditer(line)]
            reference = _REFERENCE_LINK.match(line)
            if reference:
                targets.append(reference.group(1))
            for raw_target in targets:
                target = raw_target.strip().strip("<>")
                if not target or target.startswith(("#", "//")):
                    continue
                if target.startswith(
                    ("http://", "https://", "mailto:", "tel:", "data:", "javascript:")
                ):
                    continue
                if "{{" in target or ("{" in target and "}" in target):
                    continue
                target = unquote(target.split("#", 1)[0].split("?", 1)[0])
                if not target:
                    continue
                if target.startswith("/"):
                    candidate = (root / target.lstrip("/")).resolve()
                else:
                    candidate = (path.parent / target).resolve()
                try:
                    candidate.relative_to(root.resolve())
                except ValueError:
                    problems.append(
                        LinkProblem(relative, line_number, raw_target, "link escapes repository")
                    )
                    continue
                if not candidate.exists():
                    problems.append(
                        LinkProblem(relative, line_number, raw_target, "target does not exist")
                    )
    return checked, problems


def _dotted_get(data: object, key: str) -> object | None:
    current: object = data
    for part in key.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def read_version_source(root: Path, source: VersionSource) -> str | None:
    path = (root / source.path).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise CiError(f"version source escapes repository: {source.path}") from exc
    if not path.is_file():
        return None
    if source.format == "text":
        for line in read_text(path).splitlines():
            if line.strip():
                return line.strip()
        return None
    if source.format == "regex":
        if not source.regex:
            return None
        match = re.search(source.regex, read_text(path))
        return match.group(1) if match and match.groups() else (match.group(0) if match else None)
    if source.format == "json":
        try:
            data = json.loads(read_text(path))
        except json.JSONDecodeError as exc:
            raise CiError(f"invalid JSON version source {source.path}: {exc}") from exc
        value = _dotted_get(data, source.key or "")
        return str(value) if value is not None else None
    if source.format == "toml":
        try:
            with path.open("rb") as handle:
                data = tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise CiError(f"invalid TOML version source {source.path}: {exc}") from exc
        value = _dotted_get(data, source.key or "")
        return str(value) if value is not None else None
    raise CiError(f"unknown version source format: {source.format}")


def normalize_version(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip().lstrip("vV")


def check_versions(config: Config) -> list[VersionProblem]:
    expected = normalize_version(config.project.version)
    problems: list[VersionProblem] = []
    for source in config.repository.versions:
        actual: str | None
        try:
            actual = normalize_version(read_version_source(config.root, source))
        except CiError as exc:
            problems.append(VersionProblem(source.path, expected, None, str(exc)))
            continue
        if actual is None:
            problems.append(VersionProblem(source.path, expected, None, "version not found"))
        elif expected is not None and actual != expected:
            problems.append(
                VersionProblem(
                    source.path,
                    expected,
                    actual,
                    f"version mismatch ({actual} != {expected})",
                )
            )
    return problems


def check_tracked_files(
    root: Path,
    tracked: list[str],
    *,
    forbidden_patterns: list[str],
    max_file_bytes: int,
    conflict_markers: bool,
) -> dict[str, object]:
    forbidden: list[str] = []
    large: list[tuple[str, int]] = []
    conflicts: list[str] = []
    unreadable: list[str] = []
    for relative in tracked:
        if _matches(relative, forbidden_patterns):
            forbidden.append(relative)
            continue
        path = root / relative
        try:
            size = file_size(path)
        except OSError:
            continue
        if size > max_file_bytes:
            large.append((relative, size))
        if not conflict_markers or size > 2 * 1024 * 1024 or looks_binary(path):
            continue
        try:
            text = read_text(path, limit=2 * 1024 * 1024)
        except CiError:
            unreadable.append(relative)
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            if line.startswith(_CONFLICT_MARKERS):
                conflicts.append(f"{relative}:{line_number}")
                break
    return {
        "forbidden": sorted(forbidden),
        "large": sorted(large, key=lambda item: item[1], reverse=True),
        "conflicts": sorted(conflicts),
        "unreadable": sorted(unreadable),
    }


def check_required_paths(root: Path, required: list[str]) -> list[str]:
    return [relative for relative in required if not (root / relative).exists()]


def check_lockfiles(root: Path, required: list[str], tracked: list[str]) -> dict[str, list[str]]:
    missing: list[str] = []
    untracked: list[str] = []
    tracked_set = set(tracked)
    for relative in required:
        if not (root / relative).is_file():
            missing.append(relative)
        elif relative not in tracked_set:
            untracked.append(relative)
    return {"missing": missing, "untracked": untracked}
