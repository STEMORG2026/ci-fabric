# ci-fabric 0.13.0

Hosted CI is an adapter, not a fork.

## Added

- **`.github/workflows/ci.yml` and `release.yml`** — thin GitHub Actions
  templates. `ci.yml` runs on push/PR: full-history checkout, Python 3.11,
  `./scripts/bootstrap.sh`, then the canonical `./ci ci --clean-env`, uploading
  `.ci/reports/`. `release.yml` runs on a `v*` tag and invokes `./ci release
  --clean-env --tag`, uploading `.localci/releases/`. The workflows contain no
  CI logic of their own; the orchestrator, gates, policy and reports are
  byte-identical to a local run. Treat them as templates and adapt to your
  runner.

  > Historical correction (2026-11-05): the shipped tag workflow invokes
  > `--clean-env release` (no `--tag`); the tag already exists when the
  > workflow runs. The adapter was hardened in 0.14.0 (Python matrix,
  > tag↔VERSION gate, resolved signing identity, GitHub Release publication).

# ci-fabric 0.12.0

Package outputs are cacheable.

## Added

- **`[stages.package].cache = true`** — content-addressed storage for package
  outputs. A clean tree restores the archive plus its SBOM, provenance,
  manifest and checksums instead of rebuilding them; a dirty tree is never
  looked up or stored. Keyed on the git revision and a digest of the package
  inputs and configuration, reusing the build cache's machinery (digest-verified
  restore, opt-in, clean-tree-only). Only the deterministic archive strategy
  is cacheable; command-based packaging runs its own commands and is not
  replayable from a digest.

# ci-fabric 0.11.0

A default signer: cosign keyless, invoked not embedded.

## Added

- **`[release].signer = "cosign-keyless"`** — a first-class default signer.
  ci-fabric invokes `cosign sign-blob --bundle` at release time and
  `cosign verify-blob` at release and deploy time, through the same strict
  contract as a bring-your-own signer: a failed sign aborts the release,
  checksums are re-verified after signing, the signature is self-tested before
  publish, and deploy refuses an unverifiable bundle. ci-fabric manages no
  keys and embeds no crypto; cosign owns identity through ambient OIDC
  (GitHub Actions, Google, `SIGSTORE_ID_TOKEN`, …). Configure
  `signing_identity` and `signing_issuer`; the identity, issuer, bundle filename
  and verify command are recorded in the checksummed `release.json` and reused
  at deploy time.
- `./ci doctor` reports `release.cosign` and `release.cosign-identity` when the
  default signer is enabled.

## Changed

- Unsigned releases are now recorded as `"none (no signer configured)"` (was
  `"none (no sign_command configured)"`); the label names the state, not the
  mechanism.

# ci-fabric 0.10.1

An image deployment says what it is.

## Fixed

- **`./ci status` and `./ci deploy-status` name the image.** An `oci`
  environment used to print `local: 1.0.0 (running)` with an empty URL — the one
  fact that identifies the deployment, its digest, was invisible. Both commands
  now report the strategy and a `target`: the URL for `process`/`static`, the
  immutable `tag@digest` reference for `oci`. The `deploy-status --json`
  environments list gains `strategy` and `target`; `base_url` stays, empty for
  `oci`, because nothing is listening and an invented URL would be a lie.
- `./ci deploy` no longer prints a dangling `at` when the strategy has no URL:
  it prints the reference instead.

# ci-fabric 0.10.0

A third deploy strategy: deterministic OCI images, built without a daemon.

## Added

- **`[deploy.<env>].strategy = "oci"`** — wraps the verified release artifact in
  an OCI image layout written by ci-fabric itself (blobs by digest, canonical
  JSON config/manifest/index, `oci-layout` 1.0) plus a runtime-loadable
  `image.tar`, from the same extracted bytes `static` and `process` deploy.
  Nothing is started and no port is bound. Activation builds the image,
  re-verifies the digest chain from `index.json` down to every blob, records the
  manifest digest as the deployment's identity, and only then runs the optional
  `image_command` handoff (bring-your-own `docker load`/`podman load`/`crane
  push`), which fails the deploy when it fails. Decisions.md D19.
- **`image_tag`, `image_platform`, `image_entrypoint`, `image_command`** —
  tag (default `<project>:<version>`, placeholders allowed), `linux/amd64` or
  `linux/arm64`, the image entrypoint, and the runtime handoff command, which
  receives `{image}`, `{image_tag}`, `{image_digest}`, `{image_dir}` and
  `{image_archive}`.

## Changed

- **`health`, `smoke`, `status` and `rollback` are strategy-aware.** For `oci`
  they re-prove the layout's digest chain instead of probing HTTP; rollback
  restores proved bytes and refuses a layout that no longer matches its
  recorded digest. `deploy-status --json` reports `image` for these
  environments.
- **`invariants` run for every strategy.** They were inlined in the
  process/static activation path; they are now one shared step, so an `oci`
  deployment cannot skip the system-state contract.
- **Misplaced deploy keys are configuration errors.** `port`, `start_command`
  and `server_command` are rejected with `strategy = "oci"`; `image_*` keys are
  rejected on any other strategy. A key that cannot take effect is a lie in the
  configuration, not a harmless extra.
- Auto-rollback now triggers when the previous deployment was live under *any*
  strategy; before, it required a PID, which only `process`/`static` have.
- Test count 234 → 248.

# ci-fabric 0.9.0

A suspended stage must say who suspended it and why.

## Added

- **`[stages.<name>].disabled_reason`** — required whenever a stage sets
  `enabled = false`, and rejected while the stage is enabled (a reason for a
  running stage is a lie in the configuration). The recorded reason is echoed
  in the pipeline line (`skip lint: disabled: …`), carried in the
  `<stage>.disabled` gate's summary and `details`, written into the result
  document, reported by `./ci policy`, and listed under **Suspended stages** in
  the run summary. The rationale and rejected alternatives are decisions.md D18.

## Changed

- **A stage can no longer be disabled silently.** `enabled = false` without
  `disabled_reason` is now a configuration error with the one-line fix in the
  remediation. No configuration in this workspace disables a stage, so nothing
  else had to change; the schema version stays 1 because no key changed
  meaning — only the undocumented state is refused.
- The `repository.disabled` gate built its own SKIP by hand; it now uses the
  shared `disabled_gate`, so every stage reports a suspension identically.
- Test count 229 → 234.

# ci-fabric 0.8.1

Attestation honesty: say what the bundle proves, and nothing more.

## Fixed

- **`release.json` no longer labels a checksummed provenance document
  "unsigned".** `provenance.json` and `sbom.cdx.json` are copied into the
  bundle and listed in `SHA256SUMS` — always were — so a configured
  `sign_command` covers them exactly like the artifact bytes, and a tampered
  provenance document fails both the release re-verification and the
  deploy-time check. The label now distinguishes the two real states:
  `provenance.json (covered by signed SHA256SUMS)` when a signer runs, and
  `provenance.json (listed in SHA256SUMS; no signer configured)` otherwise.
- **`security.md` stops claiming an open gap that was already closed.** The
  honest-limitation entry said binding provenance into the signed checksum set
  "is the natural next step"; the binding had shipped. The entry now states the
  actual remaining gap — authorship and independent rebuild, not integrity —
  and is pinned by tests that read `SHA256SUMS` out of a produced bundle, both
  signed and unsigned, instead of trusting the prose.

# ci-fabric 0.8.0

Coverage measurement becomes declarative: tiers say they measure, the runner does it.

## Added

- **`stages.<tier>.measure_coverage = true`** for `unit integration contract e2e`.
  When set, the runner wraps that tier's command under coverage (branch config
  from `pyproject.toml [tool.coverage.run]`), exports its parallel data file
  into this run's artifact directory, and the `coverage` stage combines every
  tier that measured into one report instead of re-running the suite. No
  `coverage run` wrapper is needed in the tier command, and `COVERAGE_FILE`
  plumbing is owned by the runner. The config loader rejects a `coverage` stage
  with `min_percent` and neither a command nor a measuring tier, so the
  "forgot to measure" case fails at load with the remedy in the message.
  Standalone `./ci coverage` (no measuring tier in the run) keeps its own
  `command(s)` as the fallback path.

## Changed

- This repository's `ci.toml` now uses `measure_coverage` on all four test
  tiers; the coverage stage no longer re-runs the suite in the pipeline path.
- Test count 216 → 229.

# ci-fabric 0.7.1

Clean build outputs: a bundle is a function of the commit, not of the machine.

## Fixed

- **Declared build outputs are emptied before the build runs.** `outputs` is a
  promise about what *this* revision produces, so whatever already sits in
  those paths belongs to an earlier build. Most toolchains append —
  `python -m build --outdir dist` never removes the previous release's files —
  and the residue was then packaged, checksummed and released as part of the
  new bundle: the 0.7.0 bundle in this repository's own `.localci/releases/`
  carried distributions for 0.1.0 through 0.6.1 alongside 0.7.0, so its digest
  depended on the machine's build history and no fresh clone of the same commit
  could reproduce it. The `build.clean` gate records exactly what was removed,
  and a cache hit is restored into an emptied output directory, so a hit and a
  fresh build yield the same tree.
  Deletion stays conservative: the checkout root, anything outside the
  repository, and ci-fabric's own state directories (`.ci`, `.localci`) are
  refused as configuration errors — `build.clean` fails and the build does not
  run — and a path tracked in Git is reported by `build.clean.tracked` and left
  in place, because a committed file is a source, never a build product.
- **Duplicated changelog sections are a build failure.** Every release from
  0.4.0 shipped a `RELEASE_NOTES.md` in which the previous version's heading
  appeared twice — the residue of prepending a section by re-emitting the
  anchor line — and because the changelog is checksummed into the bundle, the
  defect travelled inside the verified artifact. The new
  `repository.release-notes` gate parses the release headings (`# name 1.0.0`
  and Keep-a-Changelog `## [1.0.0] - date`, ignoring deeper and fenced
  headings) and fails on a repeat with the line to delete; the five duplicated
  headings are removed. Detected by reading the artifacts, not the tests: the
  analysis is recorded in decisions.md D16.

# ci-fabric 0.7.0

Stateful-system invariants: system state is a deploy gate.

## Added

- **`[deploy.<env>].invariants`** — commands that must hold on every deploy
  (migrations applied, queues empty, flags present). A failed invariant
  blocks activation exactly like a failed health check — automatic rollback
  included — and `./ci smoke` re-proves them on every invocation, making the
  system state a standing contract rather than a deploy-time formality.
  The distinction from smoke_commands is the claim under test: smoke proves
  the application answers; invariants prove the state around it. Proven by
  an end-to-end test where a release ships healthy HTTP but a missing
  migration marker, and the invariant alone triggers rollback to the
  previous good release.

# ci-fabric 0.6.1

Consumer-feedback fix from the first real adoption.

## Fixed

- `.env.example`, `.env.sample`, and `.env.template` are no longer flagged
  by the `repository.forbidden-files` gate: they are the established
  convention for documenting *which* variables a project needs, with
  placeholder values and no secrets. Exactly those three names are exempt;
  `.env`, `.env.local`, `.env.production` and every other `.env.*` stay
  forbidden. Found by Universal_Software_Auditor's adoption run, which
  tracks a legitimate `.env.example` and was correctly-but-unhelpfully
  blocked by the default `.env.*` glob.

# ci-fabric 0.6.0

Content-addressed build cache: same clean tree, same commands, no rebuild.

## Added

- **`[stages.build].cache`** (opt-in, enabled for this repository): a green
  build's declared outputs are stored under
  `.localci/cache/build/<sha256(revision, commands)>`. A later run at the
  same revision with identical rendered commands restores them digest-verified
  and skips the build command — the `build.cache` gate says "cache hit" and
  the build-marker of the dogfood test proves the command did not run.
  Dirty trees are never consulted nor stored (a dirty build is not
  reproducible); failures are never cached; corruption is a miss, never a
  wrong artifact. Keying on the revision means any commit naturally
  invalidates; the rendered command embedding the interpreter path means a
  different venv naturally misses.

# ci-fabric 0.5.0

Baseline-gated secret scanning: legacy debt is visible, new debt blocks.

## Added

- **`[stages.security].baseline`** — a committed file of finding fingerprints
  produced by `./ci security --update-baseline` (the same scanner the gate
  runs, so accepting today's findings can never accept tomorrow's). New
  findings block; accepted findings are echoed on every run and recorded in
  the gate details as visible debt; baseline entries that no longer fire are
  reported so the baseline shrinks when debt is fixed. A configured baseline
  that is missing fails the gate rather than reading as empty. The allowlist
  stays what it always was — permission for a *shape* — while the baseline is
  bookkeeping for exact *instances*. Pattern adopted from
  Universal_Software_Auditor's `--baseline` gate; implemented natively
  against ci-fabric's own finding fingerprints.

# ci-fabric 0.4.0

Gate outcomes tell the truth; test tiers run concurrently.

## Added

- **Three gate outcomes** (pattern adopted from Universal_Software_Auditor's
  gate records): a missing tool, a command that never started, or a timeout
  records `ERROR` with `details.outcome = "unverified"` — the *absence* of a
  verdict — while genuine failures stay `FAIL`. "pytest is not installed"
  can never be recorded as "pytest found failing tests". Blocking
  enforcement is unchanged; only the machine-readable label improves.
- **`[policy].parallel_test_tiers`** (opt-in, enabled for this repository):
  the contiguous unit/integration/contract/e2e block runs in a thread pool.
  The result document keeps canonical order and records what ran in
  `metadata.parallel_stages`; coverage data files are per-process by
  construction, so concurrent tiers combine cleanly. Self pipeline wall
  clock: 3m28s -> 1m48s.

# ci-fabric 0.3.0

Documentation becomes top-tier and self-enforcing; the command surface gains
a generated reference that cannot rot.

## Added

- `./ci docs [--check]`: regenerates `docs/reference.md` — the command table,
  canonical stage order, exit codes, profiles, and placeholders — from the
  live code (command tables moved to `ci_fabric.commands`, the single source
  consumed by both the parser and the generator). A contract test pins the
  tables to the parser exactly; the reference is deterministic and
  version-stamped.
- `[repository].generated_checks` is wired into this repository's pipeline:
  every run regenerates the reference and **fails on drift**. Documentation
  rot is now a build failure, not a review comment. (The gate stores clean
  paths, not porcelain codes, in its evidence.)
- Full documentation set: `docs/index.md` (index + freshness policy),
  `docs/decisions.md` (ADRs with rejected alternatives), `docs/testing.md`
  (the four-tier strategy), `SECURITY.md`, `CONTRIBUTING.md`,
  `CODE_OF_CONDUCT.md`, versioning/deprecation policy in `docs/roadmap.md`,
  and a MACP gate-interop note in `docs/adoption.md`.
- Documentation governance declared: `autodoc.toml` (AutoDOC phase beta,
  beta-ready 16/16, zero required findings — every mapping points at an owned,
  pipeline-verified file) and `CROSS-PROJECT-FINDINGS.md`, the read-only
  cross-project audit of MACP, Universal_Software_Auditor and AutoDOC with
  baselines for hourly delta cycles.

## Fixed

- `__version__` now resolves from the VERSION file in checkouts (it said
  0.1.0 through the 0.2.0 release — caught by the freshly generated
  reference) and from installed metadata in wheels.

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
