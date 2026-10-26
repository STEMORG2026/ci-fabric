# ci-fabric 0.2.0

Release signing, and coverage measurement that does not run the suite twice.

## Added

- Bring-your-own release signing: `[release].sign_command` /
  `verify_command`. A failing signer aborts the release; a signer that
  rewrites checksummed content is rejected; the signature is self-tested at
  release time and verified as a blocking check before any deploy extracts
  the bundle. `release.json` records the honest signing status. Works with
  gpg, cosign, minisign, or any exit-0 tool; ci-fabric still ships zero
  runtime crypto.

## Changed

- Test tiers now measure their own coverage into per-run parallel data
  files; the coverage stage combines them instead of re-running the entire
  suite, roughly halving full-pipeline time in this repository. A
  self-contained fallback keeps a standalone `./ci coverage` working.
- SBOMs now include hash-pinned `requirements*.lock` toolchains (this
  repository's SBOM grew from 1 to 47 components).
- `--json` outputs never mix human progress text into stdout; `hooks
  status` reports lowercase booleans; `./ci version` fixed.

## Fixed (found by dogfooding)

- `**` globs now span zero or more directories in `test_globs`/`link_globs`
  (previously `tests/unit/**/*.py` matched nothing).
- Generated/scaffolded commands use the `{python}` placeholder instead of
  bare `python`, and symlinked venv interpreters are no longer resolved in
  a way that strips the venv from subprocesses.
- Dependency audit findings fixed: pytest 8.4.2 -> 9.1.1 (PYSEC-2026-1845),
  setuptools 80.10.2 -> 84.2.1 (PYSEC-2026-3447, GHSA-xmj3-9gf7-h52w).
- Corrupt TOML lockfiles degrade to SBOM warnings instead of crashing the
  package stage; XML report parsing rejects DTD/entity payloads.

# ci-fabric 0.1.0

Initial local-first CI/CD infrastructure release.

## Included

- Canonical `./ci` entry point with `validate`, `check`, `test`, `build`,
  `package`, `security`, `ci`, `release`, and `deploy` commands.
- Strict `ci.toml` schema with explicit BLOCKING/WARNING classification.
- Repository integrity gates, format/lint/typecheck stages, layered test
  stages, coverage thresholds, secret scanning, SAST, dependency audit,
  deterministic packaging, SBOM, provenance-shaped metadata and checksums.
- Verified local release bundles under `.localci/releases/`.
- Local deployment with health/readiness/smoke checks, per-environment state,
  explicit rollback and automatic rollback after a failed activation.
- Chained Git hooks that preserve pre-existing hooks.
- Structured JSON results, Markdown summaries, run retention and logs.

## Known limitations

- POSIX only (Linux/macOS); Windows is not supported.
- Local deployment targets a single machine; there is no cluster scheduler.
- Signing, attestation and remote artifact verification are not implemented.
- Hosted CI adapters are documented but not implemented in this release.
