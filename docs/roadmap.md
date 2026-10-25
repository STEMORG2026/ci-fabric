# Roadmap

Guided by one principle: **local-first is the product, not a limitation**.
Anything that requires a hosted service stays out of the core; anything that
makes the local pipeline more rigorous comes in.

## Near term

- **Release signing and attestation (top priority).** The provenance document
  is SLSA-shaped but unsigned, and bundle verification is checksum-based.
  Cosign/Sigstore keyless signing of the release `SHA256SUMS` + provenance, and
  verification at deploy time, close the last integrity gap between "the bytes
  are intact" and "these bytes are the ones that were built".
- **A hosted-CI adapter, not a hosted-CI fork.** When GitHub Actions (or any
  other runner) enters the picture, it must invoke the same canonical commands
  (`./ci ci`, `./ci release`) in a clean checkout. The orchestrator, gates,
  policy, and reports stay byte-identical; the adapter contributes only a
  machine and a secret store. No CI logic will ever be duplicated across local
  and hosted paths — that duplication is precisely what this project exists to
  prevent. (OpenCode-hosted CI note: no workflow files are created in this
  repository by design in the current phase.)
- **Stage parallelism.** Test tiers are independent and could run
  concurrently behind the same gate/report tree. The result document already
  records per-stage durations, so speedup is measurable and honest.

## Medium term

- **Artifact cache.** Content-addressed storage for package outputs so
  unchanged trees skip rebuilds; keyed on the revision and input digests the
  manifest already records.
- **Generic container strategy.** A third `deploy` strategy (today: `process`
  and `static`) that wraps the same release bundle in an OCI image built
  deterministically, without requiring Docker for consumers who do not want it.
- **Stateful-system checks.** The deploy stage already verifies
  health/readiness; extend with declared post-deploy invariants (migrations
  applied, feature flags present, queues empty) as first-class gates with the
  same blocking semantics.
- **Coverage measurement ergonomics.** The `coverage` stage currently requires
  explicit subprocess opt-in (see this repository's `ci.toml` for the working
  pattern); a first-class `stages.coverage.measure_subprocess = [...]` option
  would remove the helper-script ceremony.

## Explicitly not on the roadmap

- **A dashboard, a daemon, or an account system.** The result documents are
  the API; render them with whatever you like.
- **Installing or guessing your toolchain.** Detection writes conservative,
  reviewed-by-you commands. ci-fabric never installs tools for you.
- **Windows support.** The runner is POSIX-oriented (process groups, flock,
  `/bin/sh`); porting is not planned unless a contributor owns it end-to-end.
- **Magic thresholds.** Every number that gates anything lives in `ci.toml`
  and changes only through review.

## Status honesty

This project is **Beta**: the gate model, packaging, release, deploy, and
rollback paths are implemented and self-tested (163 tests across four tiers,
full pipeline green on this repository), but signing is absent, hosted-CI
execution is deliberately deferred, and stage parallelism does not exist yet.
Treat it as a strong local CI with an explicit, documented gap list — not as a
finished enterprise platform.
