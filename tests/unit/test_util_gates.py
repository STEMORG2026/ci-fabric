"""Unit tests for shared utilities and gate/result primitives."""

from __future__ import annotations

import json
import stat
import tarfile
import zipfile
from pathlib import Path

import pytest

from ci_fabric import util
from ci_fabric.errors import CiError, ConfigError
from ci_fabric.gates import (
    classify,
    derive_stage_status,
    errored,
    failed,
    gate,
    passed,
    skipped,
    stage_result,
    warning,
)


def test_duration_formatting() -> None:
    assert util.human_duration(0.0123) == "12ms"
    assert util.human_duration(1.26) == "1.3s"
    assert util.human_duration(63) == "1m 03s"
    assert util.human_duration(3660) == "1h 01m"


def test_isoformat_round_trip() -> None:
    now = util.utc_now()
    text = util.isoformat(now)
    assert text.endswith("Z")
    assert util.parse_iso(text).timestamp() == pytest.approx(now.timestamp(), abs=1)


def test_atomic_writes_and_json(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "value.json"
    util.write_json(target, {"b": 1, "a": [1, 2]})
    assert json.loads(target.read_text()) == {"a": [1, 2], "b": 1}
    assert target.read_text().endswith("\n")
    util.write_text_atomic(tmp_path / "script.sh", "#!/bin/sh\n", mode=0o755)
    assert stat.S_IMODE((tmp_path / "script.sh").stat().st_mode) == 0o755


def test_relpath_and_safe_relative_path(tmp_path: Path) -> None:
    path = tmp_path / "a" / "b.txt"
    path.parent.mkdir()
    path.write_text("x")
    assert util.relpath(path, tmp_path) == "a/b.txt"
    assert str(util.safe_relative_path("a/b")) == "a/b"
    for bad in ("/etc/passwd", "../escape", "a/../../b"):
        with pytest.raises(ConfigError):
            util.safe_relative_path(bad)


def test_hashes_and_file_metadata(tmp_path: Path) -> None:
    path = tmp_path / "data.bin"
    path.write_bytes(b"payload")
    assert util.sha256_bytes(b"payload") == util.sha256_file(path)
    digest, size = util.file_sha256(path)
    assert digest == util.sha256_file(path)
    assert size == 7
    with path.open("rb") as handle:
        assert util.hash_stream(handle) == digest


def test_truncate_slug_versions_and_merge() -> None:
    assert util.truncate("  hello  ") == "hello"
    assert util.truncate("abcdef", 5) == "ab..."
    assert util.slug(" hello world!! ") == "hello-world"
    assert util.version_key("v1.10.2") > util.version_key("1.9.9")
    assert util.merge_dicts({"a": "1"}, {"b": "2"}) == {"a": "1", "b": "2"}


def test_redact_env_keeps_names_not_values() -> None:
    redacted = util.redact_env(
        {
            "PATH": "/usr/bin",
            "API_TOKEN": "supersecret",
            "LOCALCI_RUN": "abc",
            "EMPTY": "",
        }
    )
    assert redacted["API_TOKEN"] == "<redacted>"
    assert redacted["LOCALCI_RUN"] == "abc"
    assert redacted["EMPTY"] == ""
    assert "supersecret" not in json.dumps(redacted)


def test_archive_kind_and_safe_extraction(tmp_path: Path) -> None:
    assert util.archive_kind(Path("x.tar.gz")) == "tar.gz"
    assert util.archive_kind(Path("x.tgz")) == "tar.gz"
    assert util.archive_kind(Path("x.zip")) == "zip"
    assert util.archive_kind(Path("x.whl")) == "zip"
    assert util.archive_kind(Path("x.txt")) is None

    source = tmp_path / "src"
    source.mkdir()
    (source / "hello.txt").write_text("hello")
    archive = tmp_path / "good.tar.gz"
    with tarfile.open(archive, "w:gz") as handle:
        handle.add(source / "hello.txt", arcname="hello.txt")
    extracted = util.extract_archive(archive, tmp_path / "out")
    assert (tmp_path / "out" / "hello.txt").read_text() == "hello"
    assert extracted == [(tmp_path / "out" / "hello.txt").resolve()]

    with pytest.raises(CiError):
        util.extract_archive(tmp_path / "missing.zip", tmp_path / "out2")


def test_extraction_rejects_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "evil.tar"
    payload = tmp_path / "payload.txt"
    payload.write_text("evil")
    with tarfile.open(archive, "w") as handle:
        handle.add(payload, arcname="../escape.txt")
    with pytest.raises(CiError, match="escapes"):
        util.extract_archive(archive, tmp_path / "out")


def test_extraction_rejects_zip_symlink(tmp_path: Path) -> None:
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        info = zipfile.ZipInfo("link")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        handle.writestr(info, "target")
    with pytest.raises(CiError, match="symlink"):
        util.extract_archive(archive, tmp_path / "out")


def test_iter_files_and_binary_detection(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("x")
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "b.md").write_text("b")
    found = [path.relative_to(tmp_path).as_posix() for path in util.iter_files(tmp_path)]
    assert found == ["a.txt", "nested/b.md"]
    assert util.looks_binary(tmp_path / "a.txt") is False
    (tmp_path / "bin").write_bytes(b"\x00\x01")
    assert util.looks_binary(tmp_path / "bin") is True


def test_gate_classification_and_statuses() -> None:
    assert classify(True) == "BLOCKING"
    assert classify(False) == "WARNING"
    blocking = failed("a.b", "a", "b", required=True, summary="bad")
    advisory = failed("a.c", "a", "c", required=False, summary="bad")
    assert blocking.blocking_failure is True
    assert advisory.blocking_failure is False
    assert advisory.classification == "WARNING"
    assert blocking.to_dict()["status"] == "FAIL"

    gates = [passed("a.p", "a", "p", required=True)]
    assert derive_stage_status(gates) == "PASS"
    assert derive_stage_status([warning("a.w", "a", "w", summary="warn")]) == "WARN"
    assert derive_stage_status([skipped("a.s", "a", "s", summary="skip")]) == "SKIP"
    assert derive_stage_status([blocking]) == "FAIL"
    assert derive_stage_status([advisory]) == "WARN"


def test_stage_result_and_error_override() -> None:
    assert stage_result("x", []).status == "PASS"
    err = stage_result("x", [errored("x.e", "x", "e", required=True, summary="boom")])
    assert err.status == "FAIL"
    assert err.error is None
    with_error = stage_result(
        "x", [passed("x.p", "x", "p", required=True)], error="dispatch failed"
    )
    assert with_error.status == "FAIL"
    assert with_error.error == "dispatch failed"
    assert with_error.to_dict()["gates"][0]["id"] == "x.p"


def test_gate_helper_allows_explicit_info_classification() -> None:
    item = gate(
        "a.i",
        "a",
        "info",
        "PASS",
        required=False,
        classification="INFO",
        summary="just information",
    )
    assert item.classification == "INFO"
    assert item.blocking_failure is False
