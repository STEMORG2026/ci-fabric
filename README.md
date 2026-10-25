# ci-fabric

Local-first CI/CD infrastructure for any repository. One executable entry point
(`./ci`) with explicit, reproducible stages for validation, quality, tests,
security, packaging, release and local deployment.

- No hosted CI is required or contacted.
- No runtime dependencies beyond the Python 3.11+ standard library.
- All state is local: `.ci/` for ephemeral run evidence, `.localci/` for
  durable releases, deployment state and Git hooks.
- Every blocking gate exits non-zero and reports the stage, command, reason,
  log and remediation.

## Quick start

```sh
python3 -m venv .venv
.venv/bin/pip install -e /path/to/ci-fabric
./ci init
# review ci.toml, then:
./ci ci
```

`./ci init` detects supported stacks and writes conservative commands only for
tooling that is actually present. It never installs dependencies.

## Command contract

| Command | Purpose |
| --- | --- |
| `./ci validate` | repository integrity and configuration |
| `./ci check` | format, lint, type checking, secret scanning |
| `./ci test` | unit, integration, contract, e2e and coverage |
| `./ci build` | build, package and validate artifacts |
| `./ci package` | package and validate the releasable archive |
| `./ci security` | secret scanning, SAST and dependency audit |
| `./ci ci` | full pipeline |
| `./ci release` | full pipeline plus a verified local release bundle |
| `./ci deploy --env NAME` | deploy a verified release locally |

Additional commands: `doctor`, `run`, `format --fix`, `policy`, `status`,
`release`, `deploy-status`, `rollback`, `undeploy`, `health`, `smoke`,
`hooks install|uninstall|status`, `releases`, `clean`.

Exit codes: `0` success, `1` blocking gate failed, `2` configuration/usage
error, `3` missing prerequisite, `130` interrupted.

## Configuration

See `ci.toml` for this repository and `docs/` for the schema, gate rationale,
adoption guide and limitations. Unknown configuration keys are hard errors.

## Documentation

- `docs/adoption.md`
- `docs/configuration.md`
- `docs/gates.md`
- `docs/operations.md`
- `docs/security.md`
- `docs/roadmap.md`

## Development

```sh
./scripts/bootstrap.sh
./ci ci
```

The development toolchain is pinned with hashes in `requirements-dev.lock`.

## License

MIT. See `LICENSE`.
