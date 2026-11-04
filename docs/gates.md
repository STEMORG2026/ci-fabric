# Gates, classifications, and why

A **gate** is the smallest independently reportable check; a **stage** groups
gates. Only gates classified `BLOCKING` can fail the pipeline. Every blocking
failure carries: the stage, the exact command, the reason, a pointer to a log
or artifact, and a remediation hint.

## Classification rules

| classification | how a gate gets it | effect |
| --- | --- | --- |
| `BLOCKING` | the stage is `required = true` (or required by profile/policy) | failure fails the run (exit 1) |
| `WARNING` | the stage is optional | failure or warning marks the stage `WARN`, run still exits 0 |
| `INFO` | informational recorders | never blocks |

Gate status is one of `PASS`, `FAIL`, `WARN`, `SKIP`, `ERROR`. `ERROR`
means *no verdict* — the tool was missing, the process never started, or
the command timed out — never "found problems"; on a blocking stage it
blocks exactly like `FAIL` (see decisions.md D10). A run is `PASS` when no gate blocked, `WARN` when a stage warned or an
advisory gate failed, `FAIL` when a blocking gate failed. With
`fail_fast = true` (default) every stage after a blocking failure is skipped
with an explicit `skipped_reason` — a skip is always visible in the report.

## The pipeline, in order

`precheck → repository → format → lint → typecheck → unit → integration →
contract → e2e → coverage → security → audit → build → package → artifact`

Packaging precedes artifact validation deliberately: the artifact gates
verify the exact bytes that would be released or deployed, not a byproduct.

## Gate reference and rationale

| gate | stage | class | rationale |
| --- | --- | --- | --- |
| `precheck.*` | precheck | BLOCKING | config valid, git present, CI output ignored, disk headroom, supported Python, POSIX shell, environment recorded |
| `repository.tracked` / `clean` | repository | BLOCKING / WARN | a repository with no commits, or an unexpected dirty tree at release time, is not a reproducible input |
| `repository.required-paths` | repository | BLOCKING | declares the files a healthy checkout must contain |
| `repository.forbidden-files` | repository | BLOCKING | `.env`, keys, keystores, caches must never be tracked |
| `repository.conflict-markers` | repository | BLOCKING | unresolved merge markers cannot pass CI |
| `repository.large-files` | repository | BLOCKING | binary blobs over `max_tracked_file_mib` break reviewability |
| `repository.versions` | repository | BLOCKING | version drift between manifests ships the wrong version |
| `repository.release-notes` | repository | BLOCKING | a changelog that lists the same release twice ships the duplication inside the checksummed bundle; the duplicate is the signature of a prepend that re-emitted its anchor (D16). Absent changelog: SKIP — existence belongs to `require_notes` |
| `repository.lockfiles` | repository | BLOCKING | declared lockfiles must exist **and** be tracked — dependency resolution must be reproducible |
| `repository.links` | repository | WARN-able | broken relative links rot documentation |
| `repository.license` | repository | WARNING | a repository without license terms is unusable downstream |
| `repository.generated` | repository | BLOCKING | every declared generator is re-run and the tree must be unchanged — generated output that drifts from its source is a lie (D6) |
| `repository.state-ignored` | repository | WARNING | `.ci/` must be ignored, or run state and logs pollute the tracked tree |
| `<stage>.01…` | command stages | per stage | the command itself; exit code, output tail, and log recorded |
| `<test>.discovery` | test stages | BLOCKING | a configured test command that matches zero test files is a false pass waiting to happen |
| `<test>.junit` | test stages | BLOCKING | parses the JUnit report; `min_tests` enforces a real count |
| `coverage.threshold` | coverage | BLOCKING | coverage is only a gate when you set `min_percent`; `report_path` may be coverage.py XML/JSON, lcov, or a TOTAL-line command; when test tiers declare `measure_coverage`, the stage combines their run-scoped parallel data files instead of re-running the suite |
| `security.secrets` | security | per stage | tracked-content secret scan (see security.md) |
| `security.config` | security | per stage | configuration hygiene, e.g. literal credentials in `[deploy.*].env` |
| `audit.dependencies.*` | audit | per stage | dependency vulnerability audit |
| `build.outputs` | build | BLOCKING | a build that produces nothing cannot be validated, released, or deployed |
| `build.clean` | build | BLOCKING (+ WARN `build.clean.tracked`) | declared outputs are emptied before the build and before a cache restore, so a bundle is this revision's artifacts rather than this machine's build history (D15). A path tracked in Git is never deleted — it is reported as a WARNING instead |
| `build.cache` | build | opt-in | a green build's declared outputs are reused at the same revision with identical rendered commands; hits are digest-verified, misses rebuild (D13) |
| `package.*` (manifest/checksums/SBOM) | package | per stage | deterministic archive + supply-chain metadata |
| `artifact.manifest/integrity/archives/contents/secrets/smoke` | artifact | per stage | checksums match, extraction is traversal-safe, required contents exist, no secrets inside the artifact |

## Test discovery is deliberately strict

`policy.require_test_discovery = true` (default) means a configured test
command with zero matching test files **fails**. A silent zero-test pass is
the most dangerous false positive in CI. Opt out per stage with
`allow_empty = true` only with a documented reason.

## Thresholds are policy, not magic

Coverage minimums, `min_tests`, timeouts, and profile requirements all live in
`ci.toml`. Changing a threshold is a reviewed configuration change — the gate's
remediation text says so. The self-hosting configuration of this very
repository documents its own coverage floor rationale next to the value.
