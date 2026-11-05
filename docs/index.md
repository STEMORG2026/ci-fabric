# Documentation index

Everything about ci-fabric lives in one of two places: **prose docs** (reviewed
by humans, kept honest by the pipeline) and **generated docs** (regenerated
from the live code; editing them by hand is always wrong).

## Where everything lives

| Document | What it covers | Kept current by |
| --- | --- | --- |
| [README.md](../README.md) | what it is, quick start, command contract | review |
| [adoption.md](adoption.md) | install, `init`, profiles, hooks | review |
| [configuration.md](configuration.md) | every `ci.toml` key, schema 1 | review |
| [gates.md](gates.md) | every gate, its classification, and why | review |
| [operations.md](operations.md) | commands, state layout, release, deploy, troubleshooting | review |
| [governance.md](governance.md) | contribution path, security reporting, hosted protection checklist | review |
| [architecture.md](architecture.md) | module map and data flow | review |
| [security.md](security.md) | threat model, scanning, SAST suppressions, signing | review |
| [decisions.md](decisions.md) | architecture decisions with rejected alternatives | review |
| [testing.md](testing.md) | the four test tiers and what each one is for | review |
| [roadmap.md](roadmap.md) | near/medium term, explicit non-goals, deprecation policy | review |
| [reference.md](reference.md) | commands, stages, exit codes, profiles, placeholders | **`./ci docs` (generated)** |
| [../RELEASE_NOTES.md](../RELEASE_NOTES.md) | changelog and release notes | per release |
| [../CONTRIBUTING.md](../CONTRIBUTING.md) | how to contribute (CI must be green) | review |
| [../SECURITY.md](../SECURITY.md) | how to report a vulnerability | review |
| [../CODE_OF_CONDUCT.md](../CODE_OF_CONDUCT.md) | community conduct | review |
| [independent-audit-2026-11-05.md](independent-audit-2026-11-05.md) | independent audit findings CF-01…CF-29 | snapshot |
| [plans/audit-remediation-2026-11.md](plans/audit-remediation-2026-11.md) | remediation plan and execution evidence | per phase |

## Freshness policy

Documentation rot is a **build failure**, not a review comment:

1. **Generated facts cannot rot.** `docs/reference.md` is regenerated from the
   live code by `./ci docs`. The `[repository].generated_checks` gate runs the
   generator on **every pipeline run** and fails when the committed copy
   differs from what the code produces. Fix: run `./ci docs`, review the diff,
   commit.
2. **Hand-written docs are gated where they can be.** The `repository.links`
   gate verifies every relative Markdown link on every `validate`/`ci` run,
   so a renamed file immediately breaks its referrers. `required_paths`
   pins the existence of the core set.
3. **Versions cannot drift.** The `repository.versions` gate cross-checks
   `VERSION` against `pyproject.toml` on every run.
4. **What automation cannot check** (accuracy of prose, honesty of rationale)
   is a review responsibility — `CONTRIBUTING.md` states the rule: a change
   that alters documented behavior must update the doc in the same change, and
   `./ci ci` must pass.

## Companion tooling

ci-fabric cooperates with **AutoDOC** (documentation governance) by
declaration: this repository carries an `autodoc.toml` stating its phase,
kinds, traits, and where each catalog requirement is satisfied, so
`autodoc check` can audit documentation governance the same way `./ci ci`
audits the pipeline. The two tools never modify each other; the shared
surface is this repository's files.
