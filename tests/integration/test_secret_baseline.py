"""Baseline-gated secret scanning: legacy debt is accepted, new debt blocks.

The baseline is a committed list of finding fingerprints produced by the same
scanner the gate uses. New findings fail the gate; accepted findings are
recorded as visible debt; stale baseline entries (fixed debt) are reported so
the baseline can shrink. Pattern adopted from Universal_Software_Auditor's
baseline gate (see CROSS-PROJECT-FINDINGS.md).
"""

from __future__ import annotations

import json
from pathlib import Path

from ci_fabric.security import scan_repository
from tests.support import base_config, gate, make_repo, run_fixture_pipeline, stage

AWS_KEY = "AKIA" + "ABCDEFGHIJKLMNOP"


def baseline_config() -> str:
    """base_config with the baseline key added to its [stages.security] block."""
    return base_config().replace(
        "scan_config = true",
        'scan_config = true\nbaseline = "secrets-baseline.json"',
    )


def fingerprint_of(root: Path) -> str:
    report = scan_repository(root, include_untracked=False, allowlist=[])
    assert len(report.findings) == 1
    return report.findings[0].fingerprint


def write_baseline(root: Path, fingerprints: list[str]) -> None:
    (root / "secrets-baseline.json").write_text(
        json.dumps({"schema_version": 1, "accepted": [{"fingerprint": f} for f in fingerprints]}),
        encoding="utf-8",
    )


def test_new_finding_blocks_without_a_baseline(tmp_path: Path) -> None:
    root = make_repo(tmp_path, files={"leak.py": f"aws_key = '{AWS_KEY}'\n"})
    result, _context = run_fixture_pipeline(root, command="security")
    secrets = gate(result, "security.secrets")
    assert secrets.status == "FAIL"
    assert "1 new potential secret(s)" in secrets.summary


def test_baseline_accepts_legacy_finding_as_visible_debt(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        files={"leak.py": f"aws_key = '{AWS_KEY}'\n"},
        config=baseline_config(),
    )
    write_baseline(root, [fingerprint_of(root)])
    result, _context = run_fixture_pipeline(root, command="security")
    secrets = gate(result, "security.secrets")
    assert secrets.status == "PASS"
    assert "no new findings" in secrets.summary
    assert "1 accepted by baseline" in secrets.summary
    assert len(secrets.details["accepted_findings"]) == 1
    assert result.exit_code == 0


def test_new_finding_on_top_of_a_baseline_still_blocks(tmp_path: Path) -> None:
    from tests.support import commit_all, write_files

    root = make_repo(
        tmp_path,
        files={"leak.py": f"aws_key = '{AWS_KEY}'\n"},
        config=baseline_config(),
    )
    legacy = fingerprint_of(root)  # only leak.py exists right now
    write_files(root, {"leak2.py": f"github_token = 'ghp_{'B' * 36}'\n"})
    commit_all(root, "introduce a second secret")
    write_baseline(root, [legacy])
    result, _context = run_fixture_pipeline(root, command="security")
    secrets = gate(result, "security.secrets")
    assert secrets.status == "FAIL"
    assert "1 new potential secret(s)" in secrets.summary
    assert "1 accepted by baseline" in secrets.summary
    assert result.exit_code == 1


def test_fixed_debt_is_reported_as_stale_baseline(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        files={"leak.py": f"aws_key = '{AWS_KEY}'\n"},
        config=baseline_config(),
    )
    write_baseline(root, [fingerprint_of(root), "0" * 64])
    result, _context = run_fixture_pipeline(root, command="security")
    secrets = gate(result, "security.secrets")
    assert secrets.status == "PASS"
    assert "1 baseline entr(y/ies) no longer match" in secrets.summary
    assert secrets.details["stale_baseline"] == ["0" * 64]


def test_configured_but_missing_baseline_is_a_hard_error(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        files={"clean.py": "x = 1\n"},
        config=baseline_config(),
    )
    result, _context = run_fixture_pipeline(root, command="security")
    # The stage converts the CiError into an internal gate failure.
    security = stage(result, "security")
    assert security.status == "FAIL"
    assert any("baseline not found" in g.summary for g in security.gates)


def test_scanner_report_splits_fingerprints(tmp_path: Path) -> None:
    root = make_repo(tmp_path, files={"leak.py": f"aws_key = '{AWS_KEY}'\n"})
    baseline = [fingerprint_of(root)]
    report = scan_repository(root, include_untracked=False, allowlist=[], baseline=baseline)
    assert report.findings == []
    assert len(report.accepted) == 1
    assert report.stale_baseline == []
    assert report.ok is True

    report = scan_repository(root, include_untracked=False, allowlist=[], baseline=["f" * 64])
    assert len(report.findings) == 1
    assert report.accepted == []
    assert report.stale_baseline == ["f" * 64]
