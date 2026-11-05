"""Default signer story: cosign keyless (opt-in)."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from ci_fabric.errors import CiError
from ci_fabric.release import create_release
from tests.support import (
    make_repo,
    run_cli,
    run_fixture_pipeline,
    static_deploy_sections,
)

COSIGN_SHIM = """#!/usr/bin/env python3
import json
import os
import sys


def get(flag):
    if flag in sys.argv:
        return sys.argv[sys.argv.index(flag) + 1]
    for arg in sys.argv:
        if arg.startswith(flag + "="):
            return arg.split("=", 1)[1]
    return None


if "sign-blob" in sys.argv:
    bundle = get("--bundle")
    if bundle is None:
        print("missing --bundle", file=sys.stderr)
        sys.exit(2)
    with open(bundle, "w", encoding="utf-8") as handle:
        json.dump({"mediaType": "application/vnd.dev.sigstore.bundle+json;version=0.3"}, handle)
    sys.exit(0)
elif "verify-blob" in sys.argv:
    identity = get("--certificate-identity")
    issuer = get("--certificate-oidc-issuer")
    expected_id = os.environ.get("COSIGN_TEST_IDENTITY", "tester@example.com")
    expected_iss = os.environ.get("COSIGN_TEST_ISSUER", "https://issuer.example")
    if identity == expected_id and issuer == expected_iss:
        sys.exit(0)
    print(f"identity mismatch: {identity} / {issuer}", file=sys.stderr)
    sys.exit(1)
else:
    print("unknown cosign command", file=sys.stderr)
    sys.exit(2)
"""


def _make_cosign(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "cosign"
    shim.write_text(COSIGN_SHIM, encoding="utf-8", newline="\n")
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    return bin_dir


def _config(identity: str = "tester@example.com", issuer: str = "https://issuer.example") -> str:
    return (
        'changelog = "RELEASE_NOTES.md"\n'
        'signer = "cosign-keyless"\n'
        f'signing_identity = "{identity}"\n'
        f'signing_issuer = "{issuer}"\n'
    )


def _setup_env(tmp_path: Path) -> tuple[Path, str]:
    bin_dir = _make_cosign(tmp_path)
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{bin_dir}:{old_path}"
    os.environ["COSIGN_TEST_IDENTITY"] = "tester@example.com"
    os.environ["COSIGN_TEST_ISSUER"] = "https://issuer.example"
    return bin_dir, old_path


def _teardown_env(old_path: str) -> None:
    os.environ["PATH"] = old_path
    for key in ("COSIGN_TEST_IDENTITY", "COSIGN_TEST_ISSUER"):
        os.environ.pop(key, None)


def test_cosign_keyless_release_records_identity_and_bundle(tmp_path: Path) -> None:
    _, old_path = _setup_env(tmp_path)
    try:
        root = make_repo(
            tmp_path,
            files={
                "app/index.html": "<html><body>v1</body></html>\n",
                "RELEASE_NOTES.md": "# Notes\n",
            },
            sections=list(static_deploy_sections()),
            release=_config(),
        )
        _, context = run_fixture_pipeline(root, command="ci")
        create_release(context)
        bundle_dir = root / ".localci" / "releases" / "1.0.0"
        metadata = json.loads((bundle_dir / "release.json").read_text())
        assert metadata["signer"] == "cosign-keyless"
        assert metadata["signing"] == "signed (cosign keyless)"
        assert metadata["signing_bundle"] == "SHA256SUMS.sigstore.json"
        assert metadata["signing_identity"] == "tester@example.com"
        assert metadata["signing_issuer"] == "https://issuer.example"
        assert (bundle_dir / "SHA256SUMS.sigstore.json").is_file()
    finally:
        _teardown_env(old_path)


def test_cosign_keyless_deploy_verifies_and_records(tmp_path: Path) -> None:
    _, old_path = _setup_env(tmp_path)
    try:
        root = make_repo(
            tmp_path,
            files={
                "app/index.html": "<html><body>v1</body></html>\n",
                "RELEASE_NOTES.md": "# Notes\n",
            },
            sections=list(static_deploy_sections()),
            release=_config(),
        )
        _, context = run_fixture_pipeline(root, command="ci")
        create_release(context)
        env = os.environ.copy()
        result = run_cli(root, "deploy", "--env", "local", env=env)
        assert result.returncode == 0, result.stderr
    finally:
        _teardown_env(old_path)


def test_cosign_keyless_mismatched_identity_fails_release(tmp_path: Path) -> None:
    _, old_path = _setup_env(tmp_path)
    # make the shim reject the configured identity
    os.environ["COSIGN_TEST_IDENTITY"] = "other@example.com"
    try:
        root = make_repo(
            tmp_path,
            files={
                "app/index.html": "<html><body>v1</body></html>\n",
                "RELEASE_NOTES.md": "# Notes\n",
            },
            sections=list(static_deploy_sections()),
            release=_config(identity="tester@example.com", issuer="https://issuer.example"),
        )
        _, context = run_fixture_pipeline(root, command="ci")
        with pytest.raises(CiError):
            create_release(context)
    finally:
        _teardown_env(old_path)


def test_cosign_keyless_identity_from_environment_is_recorded(tmp_path: Path) -> None:
    # Hosted releases supply the per-tag identity via COSIGN_CERTIFICATE_IDENTITY
    # (a static ci.toml identity cannot match every v* tag). release.json must
    # record the RESOLVED identity, not the (absent) config value, so the
    # bundle's metadata is honest and deploy-time verification is traceable.
    _, old_path = _setup_env(tmp_path)
    os.environ["COSIGN_CERTIFICATE_IDENTITY"] = "tester@example.com"
    os.environ["COSIGN_CERTIFICATE_OIDC_ISSUER"] = "https://issuer.example"
    try:
        root = make_repo(
            tmp_path,
            files={
                "app/index.html": "<html><body>v1</body></html>\n",
                "RELEASE_NOTES.md": "# Notes\n",
            },
            sections=list(static_deploy_sections()),
            release='changelog = "RELEASE_NOTES.md"\nsigner = "cosign-keyless"\n',
        )
        _, context = run_fixture_pipeline(root, command="ci")
        create_release(context)
        bundle_dir = root / ".localci" / "releases" / "1.0.0"
        metadata = json.loads((bundle_dir / "release.json").read_text())
        assert metadata["signer"] == "cosign-keyless"
        assert metadata["signing_identity"] == "tester@example.com"
        assert metadata["signing_issuer"] == "https://issuer.example"
        assert (bundle_dir / "SHA256SUMS.sigstore.json").is_file()
    finally:
        os.environ.pop("COSIGN_CERTIFICATE_IDENTITY", None)
        os.environ.pop("COSIGN_CERTIFICATE_OIDC_ISSUER", None)
        _teardown_env(old_path)
