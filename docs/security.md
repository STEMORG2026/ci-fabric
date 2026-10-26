# Security model

ci-fabric's security posture is **local-first and fail-closed on
configuration**. This document is the honest list of what it protects against,
what it deliberately does not, and every suppressed check.

## Threat model

The orchestrator runs commands from `ci.toml`, which is **repository-authored
configuration**. Anyone who can write to the repository can run arbitrary
commands through it — the same trust boundary as a Makefile or a hosted-CI
workflow file. What ci-fabric adds is defense **inside** that boundary:

- No secrets may live in the configuration (enforced).
- No secret-shaped content may be tracked, packaged, or deployed (scanned).
- Release and deploy inputs must be verified, reproducible bytes (hashed).
- Extracted archives cannot traverse or link outside their destination.

## Secret scanning (`security.secrets`)

A credential-shaped-pattern scanner over tracked files (and untracked files
with `scan_untracked = true`), with per-rule entropy checks to cut noise.
Detected rule families include AWS keys, GitHub/PyPI/npm tokens, JWTs, and
high-entropy assignments to secret-like names. Context is redacted in every
report and result document — the finding is recorded, the secret is not.

Suppressing a finding, in order of preference:

1. Remove the credential and rotate it.
2. Inline marker: append `localci:allow-secret` to the line.
3. Precise allowlist entry: `path-glob:rule-id`, e.g.
   `tests/fixtures/*.py:github-token`. Glob semantics are path-aware
   (`**` spans directories; see configuration.md).

Findings are **BLOCKING** by default; a scan finding means the pipeline stops.

## Baseline-gated scanning (legacy debt)

Adopting secret scanning on a repository that already contains credential-
shaped content should not mean "fix everything before the first green run".
The baseline records that trade precisely:

```toml
[stages.security]
baseline = "secrets-baseline.json"
```

```sh
./ci security --update-baseline   # record current findings, then commit the file
```

The contract, and the differences from the allowlist:

| | allowlist entry | baseline entry |
| --- | --- | --- |
| what it accepts | a *shape* (path glob + rule) | an *instance* (exact finding fingerprint) |
| lifetime | permission, expected to stay | bookkeeping, expected to shrink |
| on every run | silent | echoed and recorded as visible debt |
| when the finding disappears | nothing happens | the entry is reported as stale |

New findings always block — the baseline can never accept tomorrow's secret,
because it is written by the same scanner the gate runs, at the moment you
choose to accept today's. A configured baseline that is missing fails the
gate rather than reading as empty.

## Configuration hygiene (`security.config`)

`[deploy.<env>].env` values that look like literal credentials are rejected at
configuration load time. Use `${VAR}` expansion and declare the variable in
`env_required` so a missing export fails with a clear pre-flight message
instead of a mysterious runtime error. Deploy environments receive only the
variables explicitly declared, plus a minimal runtime set.

## SAST

Bring your own static analysis as the `security` stage command — the gate
records the exit code, output, and a log. This repository dogfoods
`bandit -r src`, with these suppressions and their reasons:

| suppression | reason |
| --- | --- |
| B101 (`assert`) | assertions are a design tool in test-only and internal validation code |
| B404 (import subprocess) | the runner is a process orchestrator; every invocation is configuration-driven |
| B602/B603 (shell=True) | commands are shell strings by contract; they run in an explicitly constructed, minimal environment |
| B607 (partial path) | commands are meant to be PATH-resolved, like CI configurations everywhere |
| B310 (urllib) | deployment health probes fetch loopback URLs only, with hardcoded-safe construction |
| B104 (wildcard bind) | a deploy tool necessarily *references* wildcard addresses to map them to loopback for local probes; nothing binds them |

One `# nosec`-style suppression exists in `src/ci_fabric/scaffold.py` for a
bandit false positive (an option literally named `secret_scan`); bandit's
stderr may note the annotation's line bookkeeping, which is cosmetic.

Report parsing is hardened: XML reports (JUnit, coverage) are parsed with a
DTD/entity guard that rejects any document declaring `<!DOCTYPE>` or
`<!ENTITY>` before the standard-library parser is ever invoked, defusing
entity-expansion payloads.

## Dependency audit (`audit.dependencies.*`)

Any vulnerability scanner works as the `audit` stage command. The pipeline
fails when it reports findings — during self-CI dogfood this caught
PYSEC-2026-1845 and PYSEC-2026-3447 in this repository's own pinned toolchain,
which were then fixed and re-verified. Lockfile-based resolution
(`requirements-dev.lock`, `package-lock.json`, ...) is required for
reproducible audits.

## Supply-chain artifacts

For every packaged artifact: a deterministic archive (fixed mtimes via
`SOURCE_DATE_EPOCH`, sorted entries, fixed compression metadata), a
`package-manifest.json` with SHA-256 and sizes, `SHA256SUMS`, a CycloneDX
1.5 SBOM built from real lockfile parses (uv/poetry/requirements, npm, pnpm,
Cargo, Go), and a SLSA-shaped provenance document (in-toto Statement with
subject digests, build command, environment). Release bundles re-verify all
checksums before being published, and deploy verifies the archive again
before extraction.

## Release signing (bring your own signer)

Integrity alone (checksums) proves bytes are unmodified; signing proves
*who* vouches for them. ci-fabric deliberately ships no crypto and manages no
keys — it orchestrates your signer through a strict command contract:

```toml
[release]
# Runs inside the finished bundle (cwd = bundle) and must exit 0.
sign_command = "gpg --batch --yes --local-user ci@your.org --detach-sign SHA256SUMS"
# Runs in the same directory; exit 0 required at deploy time.
verify_command = "gpg --verify SHA256SUMS.sig SHA256SUMS"
```

Guarantees enforced by the orchestrator, not by convention:

- a failed `sign_command` aborts the release — nothing is published;
- after signing, the bundle's checksums are re-verified, so a signer that
  rewrites what the signature covers is rejected;
- `verify_command` runs at release time as a self-test, and again as a
  blocking check before any deploy extracts the bundle;
- the signature file sits outside `SHA256SUMS` (a signature covers the
  checksummed content and cannot cover itself), and `release.json` records
  `signing: "signed (external command)"` or the honest absence of a signer.

`cosign sign-blob`/`cosign verify-blob`, `minisign`, or any tool that exits 0
on success works identically.

## Honest limitations

- **No default signer and no key management.** Signing is a command
  contract; ci-fabric does not bundle crypto, create keys, or talk to a
  keyring. An unsigned configuration is recorded honestly in `release.json`
  (`"none (no sign_command configured)"`) — it proves integrity, not
  authorship.
- **Provenance is unsigned.** `provenance.json` is a SLSA-shaped in-toto
  statement, but nothing countersigns it; binding it into the signed
  `SHA256SUMS` set is the natural next step.
- **The trust boundary is the repository.** `ci.toml` is code. Review it like
  code; `init` output is deliberately conservative and must be reviewed.
- **The secret scanner is pattern-based.** It finds credential-shaped strings;
  it cannot prove a value is or is not a live credential. Findings must be
  adjudicated by humans; allowlists are auditable configuration.
- **Local runs are not hermetic.** `--clean-env` constructs a minimal
  environment instead of inheriting your shell, but a determined local process
  can still lie about its results. Local CI raises the bar; it is not a
  secure-build enclave.
- **Windows is unsupported.** POSIX only, per doctor and the runner.
