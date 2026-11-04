"""Parallel test tiers: wall-clock speedup with an unchanged result contract."""

from __future__ import annotations

import time
from pathlib import Path

from tests.support import latest_result, make_repo, run_fixture_pipeline, stage

SLEEP_SECTIONS = (
    '[stages.unit]\nrequired = true\ncommands = ["sleep 1"]\ntest_globs = ["no-tests/*.py"]\n'
    "allow_empty = true",
    '[stages.integration]\nrequired = true\ncommands = ["sleep 1"]\n'
    'test_globs = ["no-tests/*.py"]\nallow_empty = true',
    '[stages.contract]\nrequired = true\ncommands = ["sleep 1"]\n'
    'test_globs = ["no-tests/*.py"]\nallow_empty = true',
    '[stages.e2e]\nrequired = true\ncommands = ["sleep 1"]\ntest_globs = ["no-tests/*.py"]\n'
    "allow_empty = true",
)


def test_parallel_block_is_faster_and_keeps_canonical_order(tmp_path: Path) -> None:
    parallel_root = make_repo(
        tmp_path,
        name="parallel",
        policy="parallel_test_tiers = true",
        sections=SLEEP_SECTIONS,
    )
    started = time.monotonic()
    result, _context = run_fixture_pipeline(parallel_root, command="test")
    parallel_seconds = time.monotonic() - started
    assert result.exit_code == 0, result.error

    document = latest_result(parallel_root)
    names = [entry["name"] for entry in document["stages"]]
    assert names == ["precheck", "unit", "integration", "contract", "e2e", "coverage"]
    assert document["metadata"]["parallel_stages"] == [
        "unit",
        "integration",
        "contract",
        "e2e",
    ]
    for entry in document["stages"]:
        if entry["name"] in {"unit", "integration", "contract", "e2e"}:
            assert entry["status"] == "PASS", entry

    # Four 1-second sleeps in parallel must beat the sequential sum by a wide
    # margin; generous bounds keep the test stable on a loaded machine.
    assert parallel_seconds < 4.0, f"parallel run took {parallel_seconds:.2f}s"


def test_default_is_sequential(tmp_path: Path) -> None:
    sequential_root = make_repo(tmp_path, name="sequential", sections=SLEEP_SECTIONS)
    started = time.monotonic()
    result, _context = run_fixture_pipeline(sequential_root, command="test")
    sequential_seconds = time.monotonic() - started
    assert result.exit_code == 0
    document = latest_result(sequential_root)
    assert document["metadata"]["parallel_stages"] == []
    # Sequential: four 1-second sleeps dominate; the run cannot finish faster
    # than their sum.
    assert sequential_seconds >= 3.6, f"sequential run took only {sequential_seconds:.2f}s"


def test_failure_inside_parallel_block_fail_fasts_later_stages(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        name="failing",
        policy="parallel_test_tiers = true",
        sections=(
            '[stages.unit]\nrequired = true\ncommands = ["sleep 1"]\n'
            'test_globs = ["no-tests/*.py"]\nallow_empty = true',
            '[stages.integration]\nrequired = true\ncommands = ["sleep 1"]\n'
            'test_globs = ["no-tests/*.py"]\nallow_empty = true',
            '[stages.contract]\nrequired = true\ncommands = ["echo contract-boom; exit 5"]\n'
            'test_globs = ["no-tests/*.py"]\nallow_empty = true',
            '[stages.e2e]\nrequired = true\ncommands = ["sleep 1"]\n'
            'test_globs = ["no-tests/*.py"]\nallow_empty = true',
        ),
    )
    result, _context = run_fixture_pipeline(root, command="test")
    assert result.exit_code == 1
    assert result.status == "FAIL"
    # Every tier ran (the block completes in full); the failing one is red.
    assert stage(result, "unit").status == "PASS"
    assert stage(result, "contract").status == "FAIL"
    # Coverage follows the tiers and is skipped by fail-fast.
    coverage = stage(result, "coverage")
    assert coverage.status == "SKIP"
    assert coverage.skipped_reason == "fail-fast after a blocking failure"
    # The document still lists every stage in canonical order.
    names = [entry["name"] for entry in latest_result(root)["stages"]]
    assert names == ["precheck", "unit", "integration", "contract", "e2e", "coverage"]


def test_single_tier_never_parallelizes(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        name="single",
        policy="parallel_test_tiers = true",
        sections=(
            '[stages.unit]\nrequired = true\ncommands = ["sleep 1"]\n'
            'test_globs = ["no-tests/*.py"]\nallow_empty = true',
        ),
    )
    result, _context = run_fixture_pipeline(root, command="unit")
    assert result.exit_code == 0
    assert latest_result(root)["metadata"]["parallel_stages"] == []
