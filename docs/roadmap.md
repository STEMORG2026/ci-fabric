# Roadmap

Guided by one principle: **local-first is the product, not a limitation**.
Anything that requires a hosted service stays out of the core; anything that
makes the local pipeline more rigorous comes in.

## Near term

- **~Done in 0.2.0:~ release signing** ships as a bring-your-own-tool command
  contract (see security.md): a signer that fails, or rewrites checksummed
  content, or produces a signature that does not verify, aborts the release;
  deploy refuses unverified bundles. ~~**Remaining:** a first-class default
  signer story (cosign keyless).~~ ~Done in 0.11.0 (decisions.md D20):~ opt-in
  `signer = "cosign-keyless"` invokes cosign and manages no keys. The
  provenance document is already inside the signed checksum set; what the
  default signer adds is key handling and an out-of-the-box authority, not
  coverage.
- **A hosted-CI adapter, not a hosted-CI fork.** When GitHub Actions (or any
  other runner) enters the picture, it must invoke the same canonical commands
  (`./ci ci`, `./ci release`) in a clean checkout. The orchestrator, gates,
  policy, and reports stay byte-identical; the adapter contributes only a
  machine and a secret store. No CI logic will ever be duplicated across local
  and hosted paths — that duplication is precisely what this project exists to
  prevent. (OpenCode-hosted CI note: no workflow files are created in this
  repository by design in the current phase.)
- **~Done in 0.4.0:~ stage parallelism** for the test tiers
  (`[policy].parallel_test_tiers`, opt-in; this repository runs it on). A full
  stage DAG remains deliberately rejected (decisions.md D11): format/lint/
  typecheck offer ~0.4s of win for real ordering risk.

## Medium term

- ~~**Artifact cache.**~~ ~Done in 0.12.0 (decisions.md D21):~ content-addressed storage for package outputs (archive + SBOM + provenance + manifest + checksums), opt-in via `[stages.package].cache = true`, keyed on the revision and input digests. A clean tree restores the previous package instead of rebuilding; a dirty tree is never looked up or stored.
- ~~**Generic container strategy.**~~ ~Done in 0.10.0 (decisions.md D19):~
  `[deploy.<env>].strategy = "oci"` builds a deterministic OCI image layout from
  the verified release artifact in-process — no daemon required to produce it —
  verifies the digest chain before use, and hands the image to a runtime through
  an optional bring-your-own `image_command`.
- ~~**Stateful-system checks.**~~ ~Done in 0.7.0 (decisions.md D14):
  `[deploy.<env>].invariants` block activation like health failures —
  auto-rollback included — and `./ci smoke` re-proves them on every run.~
- ~~**Coverage measurement ergonomics.**~~ ~Done in 0.8.0 (decisions.md D17):~ test tiers
  declare `measure_coverage = true` and run under coverage themselves; the coverage
  stage combines their run-scoped parallel data files instead of re-running the
  suite. No `coverage run` wrapper in the tier command.
- ~~**Baseline-gated security adoption.**~~ ~Done in 0.5.0, natively, for the
  secret scan (decisions.md D12): committed fingerprint baseline,
  `--update-baseline` one-command adoption, stale-entry reporting. The
  command-based audit stage composes USA's `--baseline` directly instead.~

## Versioning and deprecation

- **Versions** are semantic (`VERSION` + `pyproject.toml`, cross-checked by the
  `repository.versions` gate on every run) and recorded per release in
  `RELEASE_NOTES.md`.
- **Configuration is versioned** (`version = 1` in `ci.toml` is mandatory).
  A schema change bumps the version and the loader refuses the old one with
  the regeneration command in the remediation — configuration never
  silently means something new.
- **Deprecations are announced in RELEASE_NOTES and kept functional through
  one minor release** with a gate-level warning (the WARNING classification
  exists for exactly this), then removed. Nothing is removed silently; removal
  without an announcement is a bug.
- **Generated docs** deprecate nothing: `docs/reference.md` is regenerated from
  the live code, so it can never describe a command that no longer exists.

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
rollback paths are implemented and self-tested (249 tests across four tiers,
full pipeline green on this repository), but signing is a bring-your-own
command contract with an opt-in cosign-keyless default signer (see security.md),
and hosted-CI execution is deliberately deferred. Treat it as a strong local CI with an explicit,
documented gap list — not as a finished enterprise platform.
