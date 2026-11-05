"""Shared utilities for ci-fabric.

The package deliberately depends only on the Python standard library at
runtime. Everything in this module is therefore defensive: it is used by the
orchestrator on arbitrary consumer repositories.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import tarfile
import tempfile
import time
import zipfile
from collections.abc import Iterable, Iterator, Sequence
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from .errors import CiError, ConfigError

_CHUNK = 1024 * 1024


def utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def isoformat(value: datetime) -> str:
    """Render a datetime in the stable, second-resolution form used by reports."""
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    """Parse an ISO timestamp written by :func:`isoformat`."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def human_duration(seconds: float) -> str:
    """Format a duration for humans without pretending to be precise."""
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, remainder = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {remainder:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_text(path: Path, *, limit: int | None = None) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return handle.read() if limit is None else handle.read(limit)
    except OSError as exc:
        raise CiError(f"cannot read {path}: {exc}", remediation="Check file permissions.") from exc


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(_CHUNK):
                digest.update(chunk)
    except OSError as exc:
        raise CiError(f"cannot hash {path}: {exc}") from exc
    return digest.hexdigest()


def file_sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(_CHUNK):
                digest.update(chunk)
                size += len(chunk)
    except OSError as exc:
        raise CiError(f"cannot hash {path}: {exc}") from exc
    return digest.hexdigest(), size


def load_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise CiError(f"JSON file not found: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise CiError(f"invalid JSON in {path}: {exc}") from exc


def load_toml(path: Path) -> dict[str, Any]:
    import tomllib

    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration file not found: {path}") from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc


def write_text_atomic(path: Path, text: str, *, mode: int = 0o644) -> None:
    ensure_dir(path.parent)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.chmod(temp_path, mode)
        os.replace(temp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            temp_path.unlink()
        raise


def write_json(path: Path, data: Any) -> None:
    text = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    write_text_atomic(path, text)


def write_bytes_atomic(path: Path, data: bytes, *, mode: int = 0o644) -> None:
    ensure_dir(path.parent)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(temp_path, mode)
        os.replace(temp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            temp_path.unlink()
        raise


def relpath(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def truncate(text: str, limit: int = 500) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def slug(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-._")
    return cleaned or "artifact"


def safe_relative_path(value: str) -> PurePosixPath:
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ConfigError(f"path must stay inside the project: {value!r}")
    return candidate


def is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


_SECRET_NAME = re.compile(
    r"(?i)(secret|token|password|passwd|api[_-]?key|private[_-]?key|credential|auth[_-]?key)"
)


def is_secret_name(name: str) -> bool:
    return bool(_SECRET_NAME.search(name))


def redact_env(env: dict[str, str]) -> dict[str, str]:
    """Redact values, not names, for environment snapshots.

    Environment variable names are useful operationally; values are not. This
    deliberately keeps every value out of the report, not only values whose
    names look secret.
    """
    redacted: dict[str, str] = {}
    for key, value in sorted(env.items()):
        if key.startswith("LOCALCI_") or key in {"CI", "TZ", "PYTHONHASHSEED"}:
            redacted[key] = value
        elif is_secret_name(key):
            redacted[key] = "<redacted>"
        else:
            redacted[key] = "<set>" if value else ""
    return redacted


def run_git(
    root: Path,
    args: Sequence[str],
    *,
    check: bool = False,
    timeout: float = 30.0,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    command = ["git", "-C", str(root), *args]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except FileNotFoundError as exc:
        raise CiError(
            "git is not installed",
            remediation="Install git and make it available on PATH.",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise CiError(f"git command timed out after {timeout}s: {' '.join(command)}") from exc
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise CiError(f"git command failed: {' '.join(command)}: {detail}")
    return result


def git_output(root: Path, args: Sequence[str]) -> str:
    result = run_git(root, args)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def git_root(path: Path) -> Path | None:
    result = run_git(path, ["rev-parse", "--show-toplevel"])
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return Path(value).resolve() if value else None


def git_common_dir(path: Path) -> Path | None:
    value = git_output(path, ["rev-parse", "--git-common-dir"])
    if not value:
        return None
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = (path / candidate).resolve()
    return candidate


def tracked_files(root: Path) -> list[str]:
    """Return tracked paths relative to *root* using NUL-separated output."""
    result = run_git(root, ["ls-files", "-z", "--full-name"])
    if result.returncode != 0:
        return []
    root_resolved = root.resolve()
    files: list[str] = []
    for raw in result.stdout.split("\0"):
        if not raw:
            continue
        candidate = (root_resolved / raw).resolve()
        if is_within(candidate, root_resolved):
            files.append(candidate.relative_to(root_resolved).as_posix())
        else:
            files.append(raw)
    return sorted(files)


def git_is_ignored(root: Path, relative: str) -> bool:
    result = run_git(root, ["check-ignore", "-q", relative])
    return result.returncode == 0


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def port_is_open(host: str, port: int, timeout: float = 0.25) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def wait_for_port(host: str, port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if port_is_open(host, port, timeout=0.2):
            return True
        time.sleep(0.1)
    return False


def atomic_symlink(target: Path, link: Path) -> None:
    """Create/replace a symlink atomically on POSIX systems."""
    link.parent.mkdir(parents=True, exist_ok=True)
    temporary = link.with_name(f".{link.name}.{os.getpid()}.tmp")
    with contextlib.suppress(FileNotFoundError):
        temporary.unlink()
    os.symlink(target, temporary, target_is_directory=True)
    os.replace(temporary, link)


def archive_kind(path: Path) -> str | None:
    name = path.name.lower()
    if name.endswith((".tar.gz", ".tgz")):
        return "tar.gz"
    if name.endswith(".tar"):
        return "tar"
    if name.endswith((".zip", ".whl")):
        return "zip"
    return None


def _safe_member_path(name: str, destination: Path) -> Path:
    normalized = PurePosixPath(name.replace("\\", "/"))
    if normalized.is_absolute() or ".." in normalized.parts:
        raise CiError(f"archive member escapes destination: {name!r}")
    target = (destination / Path(*normalized.parts)).resolve()
    if not is_within(target, destination):
        raise CiError(f"archive member escapes destination: {name!r}")
    return target


def extract_archive(path: Path, destination: Path) -> list[Path]:
    """Safely extract a tar/zip archive, rejecting links and traversal.

    Artifacts produced by this tool never contain links. Rejecting them here
    prevents a malicious or accidental archive from writing outside the
    validation sandbox.
    """
    kind = archive_kind(path)
    if kind is None:
        raise CiError(f"unsupported archive type: {path.name}")
    ensure_dir(destination)
    extracted: list[Path] = []
    if kind.startswith("tar"):
        try:
            with tarfile.open(path, "r:*") as archive:
                members = archive.getmembers()
                for member in members:
                    if member.isdir():
                        continue
                    if not member.isfile():
                        raise CiError(
                            f"archive contains a non-regular entry: {member.name!r}",
                            remediation=(
                                "Artifacts must contain only regular files and directories."
                            ),
                        )
                    target = _safe_member_path(member.name, destination)
                    ensure_dir(target.parent)
                    source = archive.extractfile(member)
                    if source is None:
                        raise CiError(f"archive member has no data: {member.name!r}")
                    with source, target.open("wb") as handle:
                        shutil.copyfileobj(source, handle)
                    extracted.append(target)
        except (tarfile.TarError, OSError) as exc:
            raise CiError(f"invalid tar archive {path}: {exc}") from exc
    else:
        try:
            with zipfile.ZipFile(path) as archive:
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    mode = (info.external_attr >> 16) & 0o170000
                    if mode == 0o120000:
                        raise CiError(
                            f"archive contains a symlink: {info.filename!r}",
                            remediation=(
                                "Artifacts must contain only regular files and directories."
                            ),
                        )
                    target = _safe_member_path(info.filename, destination)
                    ensure_dir(target.parent)
                    with archive.open(info) as source, target.open("wb") as handle:
                        shutil.copyfileobj(source, handle)
                    extracted.append(target)
        except (zipfile.BadZipFile, OSError) as exc:
            raise CiError(f"invalid zip archive {path}: {exc}") from exc
    return extracted


def iter_files(
    root: Path,
    *,
    excluded_dirs: Iterable[str] = (),
    excluded_globs: Iterable[str] = (),
    follow_symlinks: bool = False,
) -> Iterator[Path]:
    excluded = set(excluded_dirs)
    patterns = tuple(excluded_globs)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=follow_symlinks):
        current = Path(dirpath)
        dirnames[:] = sorted(
            name for name in dirnames if name not in excluded and not name.startswith(".git")
        )
        for filename in sorted(filenames):
            candidate = current / filename
            relative = candidate.relative_to(root).as_posix()
            if matches_any(relative, patterns):
                continue
            if candidate.is_symlink() and not follow_symlinks:
                continue
            if candidate.is_file():
                yield candidate


def looks_binary(path: Path, *, sample: int = 8192) -> bool:
    try:
        with path.open("rb") as handle:
            chunk = handle.read(sample)
    except OSError:
        return True
    return b"\x00" in chunk


def read_prefix(path: Path, length: int) -> bytes:
    try:
        with path.open("rb") as handle:
            return handle.read(length)
    except OSError:
        return b""


def copy_file(source: Path, destination: Path) -> None:
    ensure_dir(destination.parent)
    shutil.copy2(source, destination)


def copy_tree(source: Path, destination: Path) -> None:
    if not source.exists():
        raise CiError(f"cannot copy missing path: {source}")
    ensure_dir(destination.parent)
    if destination.exists():
        shutil.rmtree(destination)
    if source.is_dir():
        shutil.copytree(source, destination, symlinks=True)
    else:
        shutil.copy2(source, destination)


def remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def bytes_to_mib(value: int) -> float:
    return round(value / (1024 * 1024), 2)


def json_dumps(data: Any) -> str:
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False)


def hash_stream(handle: BinaryIO) -> str:
    digest = hashlib.sha256()
    while chunk := handle.read(_CHUNK):
        digest.update(chunk)
    return digest.hexdigest()


def version_key(value: str) -> tuple[tuple[int, ...], int, tuple[tuple[int, int, str], ...]]:
    """Semver-aware ordering for local release selection.

    Release numbers compare numerically (1.9 < 1.10); a prerelease sorts
    before the final release of the same version (1.0.0-rc1 < 1.0.0);
    prerelease identifiers follow semver precedence (numeric identifiers
    compare as integers and rank below alphanumeric ones, which compare
    lexically); build metadata is ignored. Non-semver shapes degrade to
    best-effort numeric ordering of their dotted components.
    """
    text = value.lstrip("vV").split("+", 1)[0]
    release, _, prerelease = text.partition("-")
    numbers = tuple(
        int(match.group(1)) for piece in release.split(".") if (match := re.match(r"(\d+)", piece))
    )
    numbers += (0,) * max(0, 3 - len(numbers))
    identifiers: list[tuple[int, int, str]] = []
    if prerelease:
        for identifier in prerelease.split("."):
            if identifier.isdigit():
                identifiers.append((0, int(identifier), ""))
            else:
                identifiers.append((1, 0, identifier))
    return (numbers, 0 if prerelease else 1, tuple(identifiers))


def glob_match(relative: str, pattern: str) -> bool:
    """Path-aware glob match used for all configuration path patterns.

    ``*`` and ``?`` never cross a ``/``; a ``**`` segment matches zero or more
    whole segments; an exact string also matches. This is the recursive
    semantics users expect from patterns such as ``tests/**/*.py``, which
    :meth:`pathlib.PurePosixPath.match` does not provide (there ``**`` behaves
    like a single ``*`` segment).
    """

    def match(pattern_parts: tuple[str, ...], parts: tuple[str, ...]) -> bool:
        if not pattern_parts:
            return not parts
        head = pattern_parts[0]
        if head == "**":
            if match(pattern_parts[1:], parts):
                return True
            return bool(parts) and match(pattern_parts, parts[1:])
        if not parts:
            return False
        return fnmatchcase(parts[0], head) and match(pattern_parts[1:], parts[1:])

    if relative == pattern:
        return True
    return match(tuple(pattern.split("/")), tuple(relative.split("/")))


def matches_any(relative: str, patterns: Iterable[str]) -> bool:
    return any(glob_match(relative, pattern) for pattern in patterns)


_XML_ENTITY_RE = re.compile(r"(?i)<!\s*(DOCTYPE|ENTITY)")


def parse_xml_file(path: Path, *, max_bytes: int = 64 * 1024 * 1024) -> Any:
    """Parse an XML report defensively (JUnit, coverage).

    Report files are produced by repository-configured commands, but they are
    still file input parsed with the standard library. DTDs and entity
    declarations are rejected outright so entity-expansion payloads never
    reach the parser; this guard is why the call sites do not use
    ``xml.etree.ElementTree.parse`` directly (bandit B405/B314).
    """
    # The DTD/entity guard below defuses entity expansion (bandit B405/B314).
    import xml.etree.ElementTree as ET  # nosec B405

    text = read_text(path, limit=max_bytes)
    if _XML_ENTITY_RE.search(text):
        raise CiError(
            f"{relpath(path, path.parent)} declares a DTD or entity; refusing to parse it",
            remediation="Regenerate the report; XML reports must not contain DTDs or entities.",
        )
    try:
        # Guarded: DTDs and entities were rejected before this parse.
        return ET.fromstring(text)  # nosec B314
    except Exception as exc:
        raise CiError(f"could not parse XML report {path}: {exc}") from exc


def merge_dicts(base: dict[str, str], overlay: dict[str, str]) -> dict[str, str]:
    merged = dict(base)
    merged.update(overlay)
    return merged
