# Adopting ci-fabric in any repository

ci-fabric is a local-first CI/CD orchestrator. Everything runs on your machine,
against your repository, with the same commands a future hosted-CI adapter
would call. There is no service, no account, and no network requirement beyond
installing the toolchain your own commands already need.

## Requirements

- POSIX system (Linux or macOS). Windows is not supported or tested.
- Python **3.11+** on `PATH` (the orchestrator itself is standard-library only).
- Git for the repository gates (release and deploy require it).
- Your project's own toolchain (pytest, ruff, npm, cargo, ...) — ci-fabric
  never installs or guesses tools; it runs exactly what you configure.

## Standard adoption

From inside the target repository:

```sh
python3 -m venv .venv
.venv/bin/pip install -e /path/to/ci-fabric     # editable install into the repo venv
.venv/bin/python -m ci_fabric init               # or: ci-fabric init
```

`init` writes two files:

- `ci.toml` — the full CI policy (schema version 1). **Review every command in
  it before relying on it**; it is generated from what is detected, not from
  what is wise.
- `./ci` — the canonical executable entry point, and appends `.ci/` and
  `.localci/` to `.gitignore`.

Commit both files. From then on the whole team and any automation uses:

```sh
./ci ci          # the full pipeline
./ci check       # format + lint + type analysis + secret scan
./ci test        # all test tiers + coverage
```

## Alternative installation modes

The generated `./ci` shim resolves the orchestrator in this order:

1. `<repo>/.venv/bin/python` with `ci_fabric` importable (recommended).
2. `CI_FABRIC_SRC` environment variable pointing at a ci-fabric checkout.
3. `<repo>/tools/ci-fabric/src/ci_fabric` (vendored copy).
4. A globally installed `ci-fabric` console script.

Set `CI_FABRIC_SRC=/path/to/ci-fabric` (the checkout root, **not** its `src/`)
to try ci-fabric in a repository without touching its virtualenv.

## Git hooks (optional)

```sh
./ci hooks install     # pre-commit + pre-push dispatchers
./ci hooks status
./ci hooks uninstall   # restores any previous core.hooksPath exactly
```

The dispatcher chains your pre-existing hooks: if you already have a
`core.hooksPath`, its scripts run first and must pass before ci-fabric's gates
run. Installation state is recorded in `.localci/hooks-state.json` and
uninstall restores the previous configuration precisely.

## Profiles

`init --profile <name>` sets the initial policy posture:

| profile | required stages | intent |
| --- | --- | --- |
| `minimal` | repository, security | basic integrity + secret scanning |
| `standard` | repository, security | same, with more gates enabled by detection |
| `strict` | repository, security, audit | adds mandatory dependency auditing |
| `release` | repository, security, audit | like strict, aimed at release workflows |

See [gates.md](gates.md) for what each profile requires and why.

## What you get, in one table

| need | command |
| --- | --- |
| full local pipeline | `./ci ci` |
| repository integrity | `./ci validate` |
| quality gates | `./ci check`, `./ci format [--fix]` |
| tests + coverage | `./ci test`, `./ci unit`, `./ci coverage` |
| security | `./ci security` (secrets, SAST, config), `./ci audit` (deps) |
| build + package | `./ci build` (build, package, artifact validation) |
| local release | `./ci release [--tag] [--notes-file FILE]` |
| deployment | `./ci deploy --env <env>`, `./ci rollback --env <env>`, `./ci undeploy --env <env>` |
| health & smoke | `./ci health --env <env>`, `./ci smoke --env <env>` |
| diagnosis | `./ci doctor` |

Exit codes are a contract: `0` success, `1` a blocking gate failed, `2`
configuration/usage error, `3` missing prerequisite, `130` interrupted.
