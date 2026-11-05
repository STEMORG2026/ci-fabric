# AGENTS.md

## Project

ci-fabric is a local-first CI orchestrator. Python 3.11+, no runtime dependencies.
The canonical entry point is `./ci` (a shim for `python -m ci_fabric`).

## Build / test / lint

```sh
./scripts/bootstrap.sh          # create .venv, install pinned deps
./ci doctor                    # environment diagnostics
./ci ci                        # full pipeline (format, lint, typecheck, tests, ...)
./ci ci --fix                  # auto-fix formatting
./ci release --version X.Y.Z   # create a release bundle
./ci deploy --env <name>       # deploy a release
```

## Conventions

- `ci.toml` is code: review it like code. `./ci init` scaffolds it; never hand-edit blindly.
- The trust boundary is the repository. Commands in ci.toml run in a shell; treat inputs as trusted.
- No backward-compatibility shims: today's version is the only version.
- `.ci/` and `.localci/` are gitignored state; never commit them.
- Workflow files under `.github/workflows/` are templates; adapt to your runner.

## No-go areas

- Do not bypass commit hooks (`--no-verify`) without explicit permission.
- Do not commit secrets; the security stage scans for them.
- Do not add a daemon, dashboard, or account system (explicitly out of scope).