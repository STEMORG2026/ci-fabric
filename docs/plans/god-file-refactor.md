# Plan: god-file refactor (deferred)

Status: **planned, not executed** (owner freeze on verified-green system).

## Motivation

USA flags four modules over 800 lines (ARCH-003, deferred):
- `src/ci_fabric/cli.py` (~1000 lines)
- `src/ci_fabric/config.py` (~900 lines)
- `src/ci_fabric/deployment.py` (~900 lines)
- `src/ci_fabric/supplychain.py` (~900 lines)

These are cohesive single-responsibility modules; the size is a smell, not a
correctness defect. Splitting is a refactor with real risk (20+ import sites,
252 tests) and no behavioral payoff, so it is recorded here rather than
executed unprompted.

## Proposed split (when approved)

1. `cli.py` → `cli.py` (parser + dispatch) + `cli_handlers.py` (the `_cmd_*`
   handlers). Keep `_build_parser` in `cli.py`.
2. `config.py` → `config.py` (dataclasses + load) + `config_validation.py`
   (the `validate()` bodies).
3. `deployment.py` → `deployment.py` (orchestration) + `deployment_health.py`
   (health/smoke probes) and `deployment_rollback.py`.
4. `supplychain.py` → `supplychain.py` (orchestration) + `sbom.py` +
   `provenance.py` + `archive.py`.

## Invariants to preserve

- All public names re-exported from the original module so existing imports
  keep working (`from ci_fabric.cli import _cmd_foo` still resolves).
- No behavioral change; full pipeline green before and after.
- One module split per commit, each verified by `./ci ci`.

## Decision

Recorded as D22 in `docs/decisions.md`. Not executed in this release.
