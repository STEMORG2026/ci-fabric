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

## D9 — Local-first means no hosted path exists yet, by design

**Decision.** No `.github/` workflows ship in this phase. The future adapter
invokes the same canonical commands (`./ci ci`) in a clean checkout — logic
is never duplicated between local and hosted paths.

**Rejected.** Shipping parallel workflow YAML now: it would fork the gate
logic at the exact moment the local contract is still hardening.

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

## Pending decisions (deliberately open)

- A first-class default signer story (cosign keyless) and countersigning the
  provenance document — see `roadmap.md`.
