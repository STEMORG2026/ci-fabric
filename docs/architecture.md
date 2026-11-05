# Architecture

ci-fabric is a single-process Python CLI. There is no daemon, no server, no
database. State lives in files under `.ci/` and `.localci/`.

## Components

- `src/ci_fabric/cli.py` — argument parsing and command dispatch.
- `src/ci_fabric/config.py` — `ci.toml` loader and validation (schema version 1).
- `src/ci_fabric/context.py` — run context: git state, paths, tool versions,
  placeholders for command rendering.
- `src/ci_fabric/pipeline.py` — stage orchestration, fail-fast, result writing.
- `src/ci_fabric/stages/` — one module per stage (build, package, artifact,
  tests, security, audit, coverage, deploy, release, ...). Each returns a
  `StageResult` with gates.
- `src/ci_fabric/gates.py` — gate model (pass/fail/warn/skip, blocking).
- `src/ci_fabric/deployment.py` — deploy/rollback/health/smoke for
  process/static/oci strategies.
- `src/ci_fabric/release.py` — release bundle creation, signing, checksums.
- `src/ci_fabric/supplychain.py` — deterministic archives, SBOM, provenance,
  checksums.
- `src/ci_fabric/buildcache.py` — content-addressed cache (build + package).
- `src/ci_fabric/doctor.py` — environment diagnostics.
- `src/ci_fabric/scaffold.py` — `./ci init`.

## Data flow

1. `./ci ci` loads config, creates context, runs stages in canonical order,
   writes `.ci/results/<run>.json` and `.ci/reports/<run>/summary.md`.
2. Build stage produces outputs; package stage archives them with SBOM +
   provenance + checksums; artifact stage validates.
3. `./ci release` re-runs the pipeline, then assembles a release bundle under
   `.localci/releases/<version>/` and signs it (BYO or cosign-keyless).
4. `./ci deploy` resolves a release, verifies its checksums/signature,
   extracts into an isolated deploy directory, and activates it.

## Trust boundary

The repository. `ci.toml` is code and is reviewed like code. Commands run in a
POSIX shell. ci-fabric never reads secrets from ci.toml; they come from the
environment.
