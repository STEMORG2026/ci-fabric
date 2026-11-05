# Contributing

ci-fabric development is gated by ci-fabric: the same pipeline that consumers
run (`./ci ci`) decides whether a change can be released. If it is red, the
change is not done.

## Setup

```sh
./scripts/bootstrap.sh     # creates .venv, installs the pinned toolchain
./ci ci                    # must be green before you start
```

The development toolchain is hash-pinned in `requirements-dev.lock`
(regenerate with
`uv pip compile --generate-hashes --no-strip-extras --python-version 3.11 requirements-dev.in -o requirements-dev.lock`).
`--no-strip-extras` keeps extra markers such as `coverage[toml]` pinned
verbatim, which older pip releases require in `--require-hashes` mode;
`scripts/bootstrap.sh` additionally installs the lock's pinned pip first.
Do not add unpinned dependencies.

## The rules

1. **The pipeline is the reviewer of record for gates.** `./ci ci` must pass
   on a clean, committed tree. New gate behavior ships with a test that
   watches it fail first, then pass.
2. **Generated docs never rot.** If you change commands, stages, exit codes,
   or placeholders, run `./ci docs` and commit the regenerated
   `docs/reference.md` — the `repository.generated` gate fails the pipeline
   on any other state.
3. **Prose docs change with behavior, in the same change.** If a change
   alters documented behavior, the doc update belongs to that change; the
   `repository.links` gate will catch renamed files, not stale claims —
   those are on you.
4. **Versions never drift.** Bump `VERSION` and `pyproject.toml` together
   (the `repository.versions` gate enforces it), and add a
   `RELEASE_NOTES.md` section.
5. **Signing/security posture changes need their tests updated in the same
   change** (see `tests/integration/test_signing.py`).

## Commits and releases

Conventional short-format commits; releases are cut with `./ci release`
(which runs the full pipeline first and refuses a dirty tree) after the
version bump is committed.

## Cross-project context

This repository cooperates with three sibling projects (AutoDOC, MACP,
Universal_Software_Auditor) under a strict read-only boundary:
cross-project observations live in `CROSS-PROJECT-FINDINGS.md`, findings are
reported to the owning project, and no sibling repository is ever modified.
Documentation governance for this repository is declared in `autodoc.toml`
(audit with `autodoc check --repo .`).
