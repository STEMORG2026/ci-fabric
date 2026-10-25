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
