# Decisions

Architecture decisions with the alternatives that were rejected. Each entry
records what was chosen, why, and what would have to be true to revisit it.
Format follows ADR conventions; entries are immutable once accepted — a
changed decision is a new entry that supersedes the old one.

## D1 — Stdlib-only runtime, forever

**Decision.** ci-fabric depends only on the Python 3.11+ standard library at
runtime. SAST, audit, signing, and coverage are command contracts, not
libraries.

**Rejected.** Rich third-party tooling (e.g. packaging `bandit`/`pip-audit`
as dependencies): every consumer would inherit our dependency graph and its
vulnerabilities, and a CI tool that breaks `pip install` breaks every
repository it serves. Rejected crypto libraries for signing for the same
reason — `gpg`/`cosign` are invoked, never imported.

**Revisit when.** A stdlib gap makes a core stage impossible (none known).

## D2 — Package validation precedes release and deploy

**Decision.** Canonical order runs `package` before `artifact`, and `release`
requires a full passing pipeline. Release bundles re-verify checksums; deploy
re-verifies the signature before extraction.

**Rejected.** "Build once, validate later": an artifact validated after
publication can already have been consumed. The artifact gates must judge the
exact bytes that ship.

## D3 — Gate classification is configuration, not code

**Decision.** BLOCKING/WARNING/INFO comes from `required` flags in `ci.toml`
(or the profile), not from hardcoded per-gate lists.

**Rejected.** Central gate registries with fixed severities: they encode one
team's risk tolerance into the tool. A consumer promoting lint to blocking —
or demoting coverage — is a one-line config change, reviewable in a diff.

## D4 — Fail-fast by default

**Decision.** After a blocking failure, remaining stages are skipped with an
explicit `skipped_reason` in the result document.

**Rejected.** Run-everything-and-record (MACP's model — the right choice for
*session handoff measurement*, where every gate's result is evidence). For a
pipeline, post-failure results against an invalidated tree cost minutes and
read as signal. Both models coexist correctly: a consumer can set
`fail_fast = false`.

## D5 — Release signing is a command contract (BYO signer)

**Decision.** `[release].sign_command`/`verify_command`; ci-fabric ships no
crypto. The orchestrator enforces the *process*: failed signing aborts the
release; a signer that rewrites checksummed content is rejected; the
signature is self-tested at release time and verified as a blocking check
before deploy.

**Rejected.** Built-in key management: keys in a CI tool's config are the
`.env`-file mistake at a larger scale. Also rejected unsigned-and-honest
alone — the process enforcement above is what makes an external signer
trustworthy.

## D6 — Docs that can be generated must be generated, and drift must fail

**Decision.** `docs/reference.md` is regenerated from the live command
tables, stage order, exit codes, profiles, and placeholders by `./ci docs`;
`[repository].generated_checks` runs it on every pipeline and fails on drift.

**Rejected.** Hand-maintained reference tables (they rot silently — the
`0.1.0` docs said `signing: not implemented` one release after signing
shipped) and separate doc pipelines that only run in CI (drift discovered
hours after the edit, not at the next run).

## D7 — Coverage measured in the test tiers, combined once

**Decision.** Test tiers write per-run parallel coverage data
(`COVERAGE_FILE` per stage); the coverage stage combines and reports. A
self-contained fallback keeps standalone `./ci coverage` working.

**Rejected.** pytest-cov in the pipeline (force-enables blanket subprocess
measurement whose statement-only data cannot combine with branch data — a
measured failure, fixed after it broke the self pipeline twice), and running
the suite twice (costs a full test-suite duration per pipeline).

## D8 — Exit codes are the cross-tool contract

**Decision.** `0/1/2/3/130` documented and tested; `--json` stdout is pure
JSON; gates record command + exit code + log + remediation.

**Why.** Verified this cycle: MACP (0/1/2), USA (0/1/2), and AutoDOC (0/2)
use the same convention, so any of them can be a ci-fabric gate command and
ci-fabric can be a gate in theirs (`["bash", "./ci", "ci"]` under MACP's
argv rules). See `CROSS-PROJECT-FINDINGS.md`.

## D9 — Local-first means no hosted path exists yet, by design (superseded by D23)

**Decision (historical).** No `.github/` workflows shipped in that phase. The
future adapter would invoke the same canonical commands (`./ci ci`) in a clean
checkout — logic is never duplicated between local and hosted paths.

**Rejected.** Shipping parallel workflow YAML at that time: it would have
forked the gate logic while the local contract was still hardening.

**Superseded.** The local contract hardened; D23 records the hosted adapter
that now ships (matrix CI + tag-gated signed release). The invariant survived:
the workflows contain no CI logic of their own.

## D10 — Three gate outcomes: pass, fail, and unverified

**Decision.** A missing tool (exit 127), a command that never started, or a
timeout records gate status `ERROR` with `details.outcome = "unverified"`;
a genuine failure records `FAIL`. Enforcement is identical — `ERROR` on a
blocking stage blocks exactly like `FAIL`.

**Rejected.** Keeping the conflation (both `FAIL`): "pytest is not installed"
and "pytest found failing tests" are different facts, and a result document
that cannot tell them apart reads as evidence when it is not. Pattern
adopted from Universal_Software_Auditor's gate records (see
`CROSS-PROJECT-FINDINGS.md`, cycle 2).

**Revisit when.** Never — this is strictly more information at zero cost.

## D11 — Test tiers may run concurrently; the document order never changes

**Decision.** `[policy].parallel_test_tiers` (opt-in) runs the contiguous
unit/integration/contract/e2e block in a thread pool. Stages are subprocess
executors (I/O bound), coverage data files are per-process by construction,
and the result document keeps canonical order regardless of execution order —
`metadata.parallel_stages` records what ran concurrently.

**Rejected.** A full stage DAG (format/lint/typecheck have no ordering
hazards with each other, but the win is ~0.4s and the risk is real);
process pools (no CPU-bound work to parallelize). Rejected default-on
until dogfooded: this repository opts in first, the default flips only
after it proves boring.

## D12 — Secrets baseline: accepted debt is visible, new debt blocks

**Decision.** `[stages.security].baseline` names a committed file of finding
fingerprints produced by `./ci security --update-baseline` (the same scanner
the gate runs, so accepting today's findings can never accept tomorrow's).
New findings fail the gate; accepted findings are recorded in the gate
details and echoed on every run as visible debt; baseline entries that no
longer fire are reported so the baseline shrinks when debt is fixed. A
configured-but-missing baseline is a hard error, never an empty baseline.

**Rejected.** Broader allowlist patterns for legacy findings (an allowlist is
permission — "this shape is fine, forever" — while a baseline is bookkeeping
for exact instances that should disappear). Pattern adopted from
Universal_Software_Auditor's `--baseline` gate (CROSS-PROJECT-FINDINGS.md);
implemented natively against ci-fabric's own finding fingerprints.

## D13 — Build cache keyed on (revision, rendered commands), clean trees only

**Decision.** `[stages.build].cache = true` stores a green build's declared
outputs under `.localci/cache/build/<sha256(revision, commands)>`. A later
run at the same revision with identical rendered commands restores them and
skips the build. Restores are digest-verified (corruption is a miss), dirty
trees are never consulted nor stored (a build of a tree nobody can name
again is not worth keeping), and failures are never cached.

**Rejected.** Keying on input-path digests (the revision already pins every
tracked byte on a clean tree — anything else is redundant work per run);
keying on the version string (two commits can share a version); caching
across environments (the rendered command embeds the interpreter path, so a
different venv is naturally a different key).

## D14 — Stateful invariants are deploy gates, not documentation

**Decision.** `[deploy.<env>].invariants` are commands that must hold on
every deploy (migrations applied, queues empty, flags present). They block
activation exactly like a health failure — auto-rollback included — and
`./ci smoke` re-proves them on every invocation, so they are a standing
contract rather than a deploy-time formality.

**Rejected.** Treating them as smoke_commands (smoke proves the *application
answers*; invariants prove the *system state* around it — conflating them is
how "the app is up" comes to mean "the migration ran" on no evidence);
making them advisory (a stateful system deployed with a failed migration is
worse than not deployed — that is what rollback exists for).

## D15 — Declared build outputs are emptied before the build

**Decision.** With `[stages.build].outputs` configured, the build stage removes
those paths — before the cache lookup and before the build command runs — so a
build can only produce the artifacts of the revision being built. The
`build.clean` gate records what was removed; a cache hit restores into the
emptied directory.

**Why.** `outputs` is a promise about *this* revision's build products, and
almost every toolchain appends to its output directory (`python -m build
--outdir dist` never removes the previous release's files). Left in place, that
residue is packaged, checksummed and released inside the new bundle, which
makes the bundle's contents — and therefore its digest — a function of the
machine's build history: a fresh clone and a long-lived checkout of the same
commit produce different bytes. Deterministic packaging cannot survive that,
and anything signed later would attest a bundle nobody else can reproduce.

**Rejected.** An opt-in `clean = true` key (stale content silently corrupts the
artifact rather than expressing a preference — a flag consumers must discover
in order to be correct is a flag most will never find); reporting a warning
instead of deleting (a warning does not change what gets packaged); deleting
tracked paths (a committed file is a source, not a build product — those are
reported by `build.clean.tracked` and left alone); permitting the checkout root
or `.ci`/`.localci` as an output (each such configuration would delete the
repository or the run state — refused, and the build does not run).

## D16 — The changelog's structure is a gate, not review discipline

**Decision.** The `repository.release-notes` gate parses the configured
changelog's release headings (`# name 1.0.0`, Keep a Changelog's
`## [1.0.0] - 2026-01-01`; deeper headings, version-less headings and fenced
examples are not sections) and fails when the same heading appears twice. It
runs on every pipeline, not only at release time.

**Why.** The changelog is copied into every release bundle and checksummed into
`SHA256SUMS`, so a structural defect ships inside the verified artifact. And it
did: five of this repository's own releases (0.4.0 through 0.7.0) contained a
duplicated version heading — the previous release's heading re-emitted as the
anchor while prepending the new section — discovered only by reading the
bundles months later. No code path in ci-fabric writes the repository's
changelog (the release stage reads it and writes only into the bundle), and the
history is linear with no merges, so the defect entered through a hand edit.
That is exactly the class of step a gate can own: the file has a machine-
checkable invariant (one section per release), and the failure was silent.

**Rejected.** Relying on review (five releases proved that does not hold);
checking only at release time (the file is broken from the moment it is
written, and the gate should fail on the commit that breaks it); flagging only
*adjacent* duplicate headings (the same defect with a section between them is
just as wrong, and the rule "a release appears once" is simpler to state and to
fix); requiring the newest heading to equal `project.version` (a project may
bump the version before writing notes, and a false failure on every development
run would teach people to ignore the gate).

## D17 — Tier measurement is declared, not scripted

**Decision.** A test stage (`unit integration contract e2e`) may set
`measure_coverage = true`. When it does, the runner wraps that tier's command
under coverage (branch config from `pyproject.toml [tool.coverage.run]`),
exports the parallel data file into this run's artifact directory, and the
`coverage` stage combines every tier that measured into a single report instead
of re-running the suite. When no tier measured (standalone `./ci coverage`),
the coverage stage falls back to its own `command(s)`.

**Why.** Before this, every tier's command had to be written as
`coverage run --parallel-mode -m pytest ...` with a shared `COVERAGE_FILE`,
and the coverage stage had to detect the data files with a shell `ls` guard.
That worked but was fragile: the measurement lived in the command string (easy
to drop by accident), the threshold stage re-ran the suite when a tier forgot
to measure, and the parallel data files had to be hand-plumbed into the right
directory. Declaring intent makes the invariant checkable at config load
(`min_percent` on a `coverage` stage now requires either a command or a
measuring tier) and lets the runner own the directory, the env var, and the
combine step.

**Rejected.** Keeping measurement in the command string (the previous state —
unverifiable, and the coverage stage could not tell "no data" from "forgot");
auto-measuring every test tier unconditionally (some tiers, e.g. a fast
smoke tier, are deliberately excluded from the coverage floor, and silently
including them would inflate the number); removing the standalone fallback
command (a repository with no test tiers, or a developer running
`./ci coverage` alone, still needs a self-contained path).

## D18 — A suspension must be on the record, or it is refused

**Decision.** `enabled = false` on a stage requires `disabled_reason`, and
`disabled_reason` is rejected while the stage is enabled. The reason is echoed
in the pipeline line (`skip lint: disabled: …`), carried in the
`<stage>.disabled` gate's summary and `details`, recorded in the result
document, and listed under "Suspended stages" in the run summary. A disabled
stage is still a SKIP: it never blocks, and deleting the two keys restores the
stage.

**Why.** Before this, disabling a stage produced one SKIP gate reading
`[stages.lint] enabled=false` — a fact, not a decision. Nothing said who
suspended it, when, or why, so the only way to learn was archaeology in Git
history, and nothing at all appeared in the summary or in `./ci status`. That
is the same class of defect as `--no-verify`: a gate that stops enforcing with
no readable trace. This project already states the rule for removal ("nothing
is removed silently"); disabling a stage is the removal of a check, and it had
no equivalent rule. The pattern was borrowed from Universal_Software_Auditor's
own protocol suspension (`.macp/SUSPENDED`): a marker file stating owner, date
and reason, refused rather than honoured when any field is missing, honoured
with the reason printed when complete — an anonymous bypass is exactly what
`--no-verify` already is.

**Rejected.** A free-form comment above `enabled = false` (invisible to the
result document and to every consumer that reads JSON); a WARN instead of a
SKIP (a suspension is a decision, not a degraded state — warning on it every
run teaches people to ignore warnings); an optional reason (an optional record
is an absent record: the first adopter who omits it recreates the hole);
`[suspend]` table or a marker *file* (a second place for the same truth —
ci.toml already holds the switch, and two sources drift); bumping the
configuration schema to 2 (no key changed meaning and no valid configuration
became ambiguous — only silent bypasses are refused, and refusing them at load
with the one-line fix is the honest migration).

## D19 — An OCI image is built from the artifact, not by a daemon

**Decision.** `[deploy.<env>].strategy = "oci"` wraps the verified release
artifact in an OCI image layout written by ci-fabric itself: the layer is the
same deterministic tar the package writer produces (sorted entries,
`SOURCE_DATE_EPOCH` mtimes, uid/gid 0), the config/manifest/index/`oci-layout`
documents are canonical JSON, and blobs are stored by digest under
`blobs/sha256/`. Activation builds it, re-verifies the whole chain from
`index.json` down to every blob, records the manifest digest as the deployment's
identity, and only then runs the optional bring-your-own `image_command`. No
daemon, no Docker, no network.

**Why.** The roadmap asked for a container strategy "without requiring Docker
for consumers who do not want it", and the digest chain is the part that matters:
an image digest is a promise about bytes, and the promise is cheap to verify if
you own the writer. Producing the layout in-process also keeps the deploy
strategy honest with the rest of the tool — the image is a function of the
verified release artifact (the same bytes `static` and `process` deploy), not of
a host build cache or a daemon's mood, so two machines deploying the same
release produce the same digest. Health for this strategy is the chain, not a
connection: nothing is running, and inventing an HTTP probe would be a lie.

**Rejected.** Shelling out to `docker build`/`buildah`/`podman` (a runtime
becomes a build dependency and the image stops being reproducible by
construction — the daemon's cache, timestamps and base-image drift leak in);
gzip-compressing the layer (a second digest, `diff_id`, must then be reconciled
against the compressed digest for no benefit in a local tool; uncompressed tar
is byte-identical everywhere); pretending an `oci` deployment serves HTTP so the
existing probe could be reused (it would probe a URL nothing listens on and
report a false failure — worse, someone would then add a real server to satisfy
it); rebuilding the image on rollback (a rollback must restore *proved* bytes,
and re-running a build is the one action that can silently produce different
ones).

## D20 — The default signer is cosign keyless, invoked not embedded

**Decision.** `[release].signer = "cosign-keyless"` (with `signing_identity`
and `signing_issuer`) makes ci-fabric invoke `cosign sign-blob --bundle` at
release time and `cosign verify-blob` at release and deploy time, through the
same strict contract as a bring-your-own signer: a failed sign aborts the
release, checksums are re-verified after signing, the signature is self-tested
before publish, and deploy refuses an unverifiable bundle. ci-fabric manages
no keys and embeds no crypto; cosign owns identity through ambient OIDC
(GitHub Actions, Google, `SIGSTORE_ID_TOKEN`, …). The identity and bundle
filename are recorded in `release.json` and used for deploy-time verification.

**Why.** The honest gap left by 0.8.1 was authorship: integrity alone proves
bytes are unmodified, not who vouches for them. A default signer had to add
key handling and an out-of-the-box authority without turning ci-fabric into a
crypto library or a keyring. Delegating to cosign keeps the BYO contract
machinery as the single enforcement path and reuses the same re-verify and
deploy-time gates; keyless mode removes key management entirely by binding
authorship to an OIDC identity and a transparency-log entry. Recording
`signing_identity`/`signing_issuer`/`verify_command` in the checksummed
`release.json` means a tampered identity fails the same way a tampered byte
does.

**Rejected.** Embedding a crypto library (a new dependency, a new trust root,
and a new class of vulnerability for a tool whose whole posture is "no crypto");
generating and storing long-lived signing keys (key management is the hard
part and the part that rots); making cosign the default without opt-in
(network and interactive OIDC in a local-first tool would surprise users and
hang in CI); silently falling back to unsigned when cosign is missing (that is
exactly the unprovable claim the 0.8.1 label work removed).

## D21 — Package outputs are cacheable, keyed on revision and input digests

**Decision.** `[stages.package].cache = true` (opt-in) stores the package's
archive plus its SBOM, provenance, manifest and checksums in a
content-addressed cache under `.localci/cache/package`, keyed on the git
revision and a digest of the package inputs and configuration. A clean tree
restores all of them and skips packaging; a dirty tree is never looked up or
stored. Only the deterministic archive strategy is cacheable; command-based
packaging runs its own commands and is not replayable from a digest.

**Why.** The build cache (D13) already skips identical builds; packaging
re-runs SBOM generation, provenance, manifest writing and checksums for an
unchanged tree, which is wasted work on every local run. Reusing the same
content-addressed machinery keeps one enforcement path (digest-verified
restore, clean-tree-only, opt-in) instead of a second ad-hoc cache. Caching
the derived files together means a hit is a complete, consistent package, not
just an archive with stale metadata.

**Rejected.** Caching only the archive (the derived SBOM/provenance would then
be regenerated against a restored archive, defeating the skip); caching
command-based packaging (its outputs depend on commands ci-fabric cannot
observe, so a hit could be a lie); caching on dirty trees (a package of a
tree nobody can name again is not worth keeping, and the build cache already
established this rule).

## Pending decisions (deliberately open)

- ~~A mechanical `./ci notes --add <version>` command that prepends the new
  section to the changelog.~~ **Built** as `./ci notes` (add/show/clear) in
  0.13.x (first released in 0.14.0): composes an `## Unreleased` section in `RELEASE_NOTES.md` so
  notes accumulate across commits and are cleared by `./ci notes --clear`
  after a release. D16's gate still catches a stale/missing Unreleased block;
  the composer prevents the empty-notes failure mode.

## D22 — God-file refactor is planned, not executed

**Decision.** The four modules over 800 lines (cli, config, deployment,
supplychain) are left intact in this release. A concrete split plan is
recorded in `docs/plans/god-file-refactor.md`.

**Why.** They are cohesive single-responsibility modules; the size is a smell,
not a correctness defect. Splitting is a refactor with real risk (20+ import
sites, 277 tests) and no behavioral payoff. The owner freezes verified-green
systems: improvements become plans/ADRs, not unprompted work.

**Rejected.** Splitting now (risk of breaking the green pipeline for a lint
score); ignoring the smell entirely (tracked as ARCH-003 in `.usa.yaml`).

## D23 — Hosted CI is an adapter, not a fork

**Decision.** Ship `.github/workflows/ci.yml` (push/PR, Python 3.11–3.15
matrix, `fail-fast: false`) and `.github/workflows/release.yml` (`v*` tag,
tag↔VERSION gate, cosign-keyless signing, GitHub Release publication). Both
invoke the canonical CLI in a clean environment; no gate logic lives in YAML.
Supersedes D9's "no hosted path yet".

**Why.** The local contract is stable and self-tested; teams need a hosted
signal that is byte-identical to a local run. A matrix proves the supported
Python range (3.11–3.15) rather than a single interpreter. Signing uses
GitHub Actions OIDC with a per-tag certificate identity recorded in
`release.json`, so a release is verifiable without managing keys.

**Rejected.** Duplicating gate logic in workflow steps (forks the contract);
a single-Python hosted job (hides version-specific breakage); static signing
identity in `ci.toml` (cannot match every `v*` tag); `attest-build-provenance`
as a second provenance path (the SLSA-shaped `provenance.json` is already
checksummed and signed via `SHA256SUMS`).

**Revisit when.** A consumer needs a non-GitHub host; the adapter boundary
(`./ci … --clean-env`) is the port.
