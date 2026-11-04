"""Release signing and deploy-time signature verification (BYO signer)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ci_fabric.deployment import deploy
from ci_fabric.errors import CiError
from ci_fabric.release import create_release
from tests.support import (
    make_repo,
    run_fixture_pipeline,
    static_deploy_sections,
)

# A deterministic stand-in for gpg/cosign: the "signature" is a keyed digest
# of SHA256SUMS. The mechanics under test are ci-fabric's, not the signer's.
SIGNER = """\
import hashlib
from pathlib import Path

data = Path("SHA256SUMS").read_bytes()
Path("SHA256SUMS.sig").write_text(
    hashlib.sha256(b"fixture-signer:" + data).hexdigest() + "\\n"
)
"""

VERIFIER = """\
import hashlib
import sys
from pathlib import Path

data = Path("SHA256SUMS").read_bytes()
expected = Path("SHA256SUMS.sig").read_text().strip()
actual = hashlib.sha256(b"fixture-signer:" + data).hexdigest()
sys.exit(0 if actual == expected else 1)
"""

# Signs correctly, then corrupts the checksummed content: the signer must not
# be allowed to rewrite what the signature covers.
EVIL_SIGNER = """\
import hashlib
from pathlib import Path

data = Path("SHA256SUMS").read_bytes()
Path("SHA256SUMS.sig").write_text(
    hashlib.sha256(b"fixture-signer:" + data).hexdigest() + "\\n"
)
with Path("RELEASE_NOTES.md").open("a") as handle:
    handle.write("smuggled content\\n")
"""


def make_signing_repo(
    tmp_path: Path,
    *,
    sections: list[str],
    release: str | None = None,
    files: dict[str, str] | None = None,
) -> Path:
    all_files = {"sign_bundle.py": SIGNER, "verify_bundle.py": VERIFIER}
    if files:
        all_files.update(files)
    if release is None:
        release = (
            'changelog = "RELEASE_NOTES.md"\n'
            'sign_command = "{python} {root}/sign_bundle.py"\n'
            'verify_command = "{python} {root}/verify_bundle.py"'
        )
    all_files_base = {
        "app/index.html": "<html><body>v1</body></html>\n",
        "RELEASE_NOTES.md": "# Notes\n",
    }
    all_files_base.update(all_files)
    return make_repo(
        tmp_path,
        files=all_files_base,
        sections=sections,
        release=release,
    )


def test_release_is_signed_and_records_the_status(tmp_path: Path) -> None:
    root = make_signing_repo(tmp_path, sections=list(static_deploy_sections()))
    _result, context = run_fixture_pipeline(root, command="ci")
    record = create_release(context)

    bundle = root / ".localci" / "releases" / record.version
    signature = bundle / "SHA256SUMS.sig"
    assert signature.is_file()
    assert signature.read_text(encoding="utf-8").strip()
    release_json = json.loads((bundle / "release.json").read_text(encoding="utf-8"))
    assert release_json["signing"] == "signed (external command)"
    # The signature is deliberately outside the checksum manifest: it covers
    # the checksummed content and cannot cover itself.
    sums = (bundle / "SHA256SUMS").read_text(encoding="utf-8")
    assert "SHA256SUMS.sig" not in sums
    # Provenance and the SBOM are inside the checksummed set, so the signature
    # covers them exactly like the artifact bytes.
    assert "provenance.json" in sums
    assert "sbom.cdx.json" in sums
    assert release_json["attestation"] == ("provenance.json (covered by signed SHA256SUMS)")


def test_deploy_verifies_signature_before_extraction(tmp_path: Path) -> None:
    root = make_signing_repo(tmp_path, sections=list(static_deploy_sections()))
    _result, context = run_fixture_pipeline(root, command="ci")
    create_release(context)
    try:
        outcome = deploy(context, "local")
        assert outcome.ok, outcome.error
        signature_check = next(c for c in outcome.checks if c.name == "signature")
        assert signature_check.ok
        assert signature_check.detail == "signature verified"
    finally:
        from ci_fabric.deployment import undeploy

        undeploy(context, "local")


def test_deploy_refuses_a_bundle_with_a_broken_signature(tmp_path: Path) -> None:
    root = make_signing_repo(tmp_path, sections=list(static_deploy_sections()))
    _result, context = run_fixture_pipeline(root, command="ci")
    create_release(context)
    signature = root / ".localci" / "releases" / "1.0.0" / "SHA256SUMS.sig"
    signature.write_text("0" * 64 + "\n", encoding="utf-8")

    outcome = deploy(context, "local")
    assert not outcome.ok
    assert "signature verification failed" in (outcome.error or "")
    signature_check = next(c for c in outcome.checks if c.name == "signature")
    assert not signature_check.ok
    remediation = outcome.remediation or ""
    assert "signature does not verify" in remediation
    history = (
        (root / ".localci" / "deploy" / "local" / "history.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    assert any('"error": "release signature verification failed"' in line for line in history)


def test_failed_signing_aborts_the_release(tmp_path: Path) -> None:
    root = make_signing_repo(
        tmp_path,
        sections=list(static_deploy_sections()),
        release='changelog = "RELEASE_NOTES.md"\nsign_command = "false"',
    )
    _result, context = run_fixture_pipeline(root, command="ci")
    with pytest.raises(CiError, match="release signing failed"):
        create_release(context)
    # Nothing is published when signing fails.
    assert not (root / ".localci" / "releases" / "1.0.0").exists()
    leftovers = list((root / ".localci" / "releases").glob(".*tmp*"))
    assert leftovers == []


def test_signer_that_rewrites_content_is_rejected(tmp_path: Path) -> None:
    root = make_signing_repo(
        tmp_path,
        sections=list(static_deploy_sections()),
        files={"sign_bundle.py": EVIL_SIGNER},
    )
    _result, context = run_fixture_pipeline(root, command="ci")
    with pytest.raises(CiError, match="modified checksummed bundle content"):
        create_release(context)
    assert not (root / ".localci" / "releases" / "1.0.0").exists()


def test_verify_without_sign_is_rejected_at_load(tmp_path: Path) -> None:
    root = make_repo(tmp_path, release='changelog = "RELEASE_NOTES.md"\nverify_command = "false"')
    from ci_fabric.config import load_config

    with pytest.raises(CiError, match="verify_command is configured without sign_command"):
        load_config(root)
