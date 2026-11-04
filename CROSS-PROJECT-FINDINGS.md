<!-- ci-fabric cross-project findings — written by the Local CI/CD project.
     Baselines record the commit each sibling repo was last fully read at;
     later cycles re-read via `git log <baseline>..HEAD` deltas only. -->

# Cross-project findings — from the Local CI/CD (ci-fabric) perspective

Cycle 1, 2026-10-26. Read-only audit of MACP, Universal_Software_Auditor
(USA), and AutoDOC, focused on CI/CD surfaces: gates, exit codes, pipeline
composition, drift automation, and the interfaces through which any of them
could run under — or invoke — `./ci`.

## Baselines (for hourly delta cycles)

| repo | baseline commit | first full read |
| --- | --- | --- |
| MACP | `086be27` | 2026-10-26 (all src/lib, bin, hooks, enforce/, protocol docs) |
| Universal_Software_Auditor | `eced4c9` | 2026-10-26 (CI surface: action.yml, workflows, ci-integration, gate/exit semantics, package scripts) |
| AutoDOC | `2179ecc` | 2026-10-26 (wheel CLI, autodoc.toml schema, Makefile gate composition, workflows) |

## Shared conventions (confirmed across all four projects)

**Exit codes.** `0` success · `1` gate/finding failure · `2` usage or
configuration error. Verified in ci-fabric (`errors.py`), MACP (`bin/macp.mjs`,
`enforce/*`), USA (docs/ci-integration.md), AutoDOC (`cli.py` returns 2,
Makefile `exit 2`). ci-fabric's `3` (missing prerequisite) and `130`
(interrupt) are compatible supersets. **Any of these tools can be a gate
command in any other without adapters.**

**Measured-not-asserted doctrine.** All four record exit codes, SHAs, and
durations read from execution rather than typed by an agent. ci-fabric's
result JSON, MACP's handoff (`macp-handoff-v1`), USA's `usa-report-json-v1`,
AutoDOC's baseline JSON agree on this.

**Generated-docs drift is a build failure.** Three projects converged
independently: AutoDOC (`autodoc generate --check` + `.doc-sync-map.yaml`),
USA (`docs:cli --check`, `docs:sample --check`, `docs:check` npm gates),
ci-fabric (`./ci docs` + `[repository].generated_checks`, built this cycle).
Ecosystem-level pattern worth an eventual shared note, not a shared
implementation.

## MACP — findings

What it implements: a session state machine (UNBOUND→ACTIVE→…→CLOSED) with
consumer-declared gates in `.macp/gates.json` (argv arrays, run-all-and-record
`{name, exit, ms, tail}`), SSH-signed lifecycle acts (one key per act:
close/seal/reopen/ack), out-of-tree leases, a pre-tool guard with four harness
adapters, and three CI-side structural gates (`enforce/check-session-start`,
`check-sessions`, `check-protocol`).

Interfaces relevant to ci-fabric:

1. **`./ci` as a MACP gate works today** via `["bash", "./ci", "ci"]` —
   MACP's gates.json validation requires bare command names (no `/` in
   argv[0]), so `./ci` cannot be argv[0]. Not a defect on either side; the
   wrapper pattern belongs in ci-fabric's adoption docs (doing that).
2. MACP runs gates with `CI=1`, `FORCE_COLOR=0` — matches ci-fabric's stage
   environment conventions exactly.
3. Their handoff records gates as `{exit, ms, tail}`; a composite gate like
   `./ci ci` maps losslessly (name=`ci`, exit, ms; `.ci/results/latest.json`
   holds the full gate tree for humans). No protocol change needed anywhere.
4. Their seal doctrine ("audit control, not security control") is the same
   honesty language as ci-fabric's release signing; the two signatures cover
   different objects (session records vs release bundles) — complementary.

Feedback for MACP (not fixed by us, per boundary rules):

- `src/lib/lifecycle.mjs`: the `keyLoadedInAgent` refusal block is duplicated
  verbatim three times (close ~563/567, seal ~803/807, reopen ~985/989).
  Harmless at runtime; copy-paste debris worth one cleanup commit.
- Gates ergonomics: a documented `./relative` exception (or a canonical
  wrapper example for repo-local entry points like `./ci`, `./Makefile`
  targets) would remove the only friction a ci-fabric consumer hits.
- Their `dirtyPaths` porcelain-trim fix ("`state/` became `tate/`") is the
  identical defect ci-fabric fixed in its generated-drift gate this cycle;
  both fixes agree on the shape (slice by status codes, never trim first).

## Universal_Software_Auditor — findings

What it implements: a repository auditor with a severity-graded quality gate
(`--fail-on critical|high|medium|low|none`), a **baseline gate**
(`--baseline <previous report>`: only new/regressed findings block — legacy
findings pass), SARIF 2.1.0 + stable JSON outputs, a composite GitHub Action,
and 12 hosted workflows (CI, self-audit dogfood, protocol-check running
**MACP's gates**, codeql, scorecard, gitleaks, pinned-action enforcement).

Interfaces relevant to ci-fabric:

1. `usa audit --fail-on high` is consumable as a ci-fabric
   `[stages.security]`/`[stages.audit]` command **today** — exit codes align
   (0/1/2), SARIF/JSON outputs satisfy ci-fabric's "log + structured report"
   evidence model.
2. **The baseline gate is a pattern ci-fabric lacks** and should adopt for
   security/audit adoption on legacy repositories: fail only on regressions
   versus a committed baseline report. Added to ci-fabric's roadmap this
   cycle (own implementation; no coupling).
3. USA already runs MACP's `check-session-start`/`check-protocol`/
   `check-sessions` + `macp verify` in CI — proof the MACP gate scripts are
   consumable verbatim by other tooling, which de-risks any future
   `./ci`-as-gate usage in the other direction.
4. Their actions-pinning gate (`actions:check`, every `uses:` pinned to a
   commit SHA) is the correct bar for ci-fabric's future hosted adapter —
   noted in the roadmap's adapter section.

## AutoDOC — findings

What it implements: documentation governance — declaration-based context
(`autodoc.toml`: phase, kinds, traits, satisfied_by/instantiated mappings,
severity pins), a 24-type core catalog, drift checking (`generate --check`),
metadata/freshness/ownership/inventory gates composed as `make docs-verify`,
phase-scaled enforcement (`docs-enforce`), and per-event trigger model
verified against their workflows.

Interfaces relevant to ci-fabric:

1. ci-fabric consumes AutoDOC as a wheel in an isolated venv (read-only for
   AutoDOC): `autodoc check --repo .` currently reports 47 informational
   catalog recommendations and 31 "undetermined" facts. ci-fabric will answer
   with a committed `autodoc.toml` this cycle (phase: beta; kinds: application;
   traits: no payments/personal-data/safety-critical; honest
   satisfied_by/instantiated mappings to ci-fabric's docs).
2. AutoDOC's `docs-sync` (mapped source→doc co-change validation) is the
   human-docs complement to ci-fabric's generated-docs drift gate; the two
   compose without overlap (generated vs authored content).
3. Their `docs-require-check` (print or apply the required branch-protection
   check) is a good pattern for ci-fabric's future hosted adapter.

## Requirements this read imposes on ci-fabric (implemented this cycle)

1. Document the MACP gate-wrapper pattern in adoption docs.
2. Adopt the baseline-gate concept into the roadmap (security/audit stages).
3. Write the committed `autodoc.toml` answering AutoDOC's audit.
4. No exit-code or gate-contract changes: ci-fabric already conforms.

## Cycle log

- Cycle 1 (2026-10-26): first full read (see baselines). Findings above.
- Cycle 2 (2026-10-26): deltas — MACP unchanged; AutoDOC one docs commit
  (USA-audit fixes, no interface impact); USA shipped `feat(macp): a gate
  has three outcomes, not two` — missing-tool/timeout gates record
  `unverified` instead of `fail` so an unread gate can never masquerade as
  a red one. **Adopted into ci-fabric the same cycle** (decisions.md D10):
  exit 127 / never-started / timed-out gates now record `ERROR` +
  `details.outcome = "unverified"`, blocking unchanged; 4 contract tests.
  Also shipped this cycle: opt-in `parallel_test_tiers` (D11). New USA
  baseline to read at next delta: `432217e`.
- Cycle 3 (2026-10-26, later): deltas — MACP +1 (owner rulings record, no
  code); AutoDOC unchanged; USA +7, two MACP-gate fixes relevant to us as
  evidence-pattern consumers: (a) `a77a8b7` — seal verification failed on an
  *unchanged* file ~1 run in 4 (block-parser ambiguity picking the wrong
  `## Close` occurrence), found by chasing a flaky test; (b) `a0e68ec` —
  a rewritten history could close a tree that was never measured, fixed by
  comparing the *tree* the gates measured, not the commit id, so rebases
  cannot smuggle an unmeasured tree past a close. Both are lineage defects
  ci-fabric's own model avoids by construction (we re-measure the working
  tree at release time and refuse dirty trees), but the tree-vs-commit
  comparison is recorded here as the correct pattern for any
  measure-then-verify gate. New baselines: MACP `a0dac82`, USA `b012f57`.
  Shipped this cycle: 0.4.0 (unverified outcomes, parallel tiers) and
  0.5.0 (baseline-gated secrets; adoption of USA's baseline pattern,
  natively against our own finding fingerprints — decisions.md D12).
- Cycle 4 (2026-10-26, evening): **Universal_Software_Auditor adopted
  ci-fabric** — its repo carries a generated `ci.toml`, the `./ci` shim,
  and a real adoption trail in `.ci/results/` (doctor PASS → validate FAIL
  → check FAIL → test FAIL → test PASS, ending green on its 108s vitest
  suite). Two product findings from that trail, both ours to fix:
  1. `repository.forbidden-files` flagged USA's legitimate `.env.example`
     — the `.env.*` default glob catches the established documentation
     convention. **Fixed in ci-fabric**: exactly `.env.example`/`.env.sample`
     `/`.env.template` are now exempt (`.env.local` etc. stay forbidden).
  2. `security.secrets` reported 34 findings on USA's deliberately
     credential-shaped audit fixtures — precisely the legacy-adoption case
     the 0.5.0 baseline exists for. Guidance for USA (their decision, their
     repo): `[stages.security].baseline` + `./ci security --update-baseline`,
     or allowlist entries per fixture path.
  MACP +5: lease-first session start (no orphan records on lease failure —
  the same "receipt outside the tree" discipline our run-lock follows);
  Q007 logged (queue.md freeze rules disagree between gates) — theirs to
  resolve. AutoDOC +1 (USA hygiene round; no interface impact).
  Shipped this cycle: 0.6.0 (content-addressed build cache, D13), the
  `.env.example` exception, and this log. New baselines: MACP `495ef95`,
  USA `3ddd30c`, AutoDOC `4e26ca1`.
- Cycle 5 (2026-11-04): delta from `495ef95` (MACP, 245 commits),
  `3ddd30c` (USA, 10), `4e26ca1` (AutoDOC, 6). Read scope: every delta commit
  subject, plus full diffs for USA's CI surface, AutoDOC's release/adoption
  commits and the MACP modules the non-state commits touched — not all 31k
  lines of MACP's diff, most of which is `chore(state)` session churn.
  **Adopted (0.9.0):** USA's reasoned-suspension pattern (`.macp/SUSPENDED`:
  owner/since/reason, refused when incomplete, honoured with the reason printed)
  became ci-fabric's `disabled_reason` — a stage can no longer be disabled
  silently (decisions.md D18). This was a real hole on our side: disabling a
  stage produced one SKIP reading `enabled=false`, with nothing in the summary
  or the JSON saying who or why. **Second adopter confirmed:** AutoDOC carries a
  generated `ci.toml`, the `./ci` shim and a real `.ci/` trail — 17 runs ending
  PASS (48 pass / 1 skip / 0 fail) at `0460da5`, no new adoption friction; the
  trail predates their 0.4.0 release work, so it will need a re-run on their
  next release. **Not applicable:** USA's checksum-verified trivy asset fetch
  (ci-fabric does not download or install toolchains — a stated non-goal) and
  their SLSA/Sigstore provenance bundle, which is evidence for the open
  default-signer item rather than a gap in ours (our provenance is already
  inside the signed checksum set). **Consistent, no change:** MACP's
  measure-at-HEAD `recon` (one measurement authorises one session at one
  commit), its gate set frozen at authorisation (F-04/F-05), and AutoDOC's
  release gate for tag/version/changelog — ci-fabric pins the revision and
  config hash per run, refuses dirty release trees, freezes required stages per
  profile, and gates version drift + changelog structure (D16).
  **Worth a later read:** MACP's `docs/PROTOCOL.md` §1.1 rule — "gate what
  compounds, deliver what is visible, delete what is ritual" — is a sharper
  classification of pre-session steps than anything in `gates.md`; it invites
  an honest audit of whether any ci-fabric gate is ritual. Not acted on this
  cycle. New baselines are the table above.
