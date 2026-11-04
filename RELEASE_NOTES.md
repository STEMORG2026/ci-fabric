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
