# `ci.toml` reference (schema version 1)

`ci.toml` is the single, strict policy document. **Unknown keys are hard
errors** — a typo fails fast with the offending key name instead of silently
disabling a gate. The schema version must be exactly `1`; regenerate with
`./ci init --force` and review the diff when it changes.

```toml
version = 1
```

## Placeholders

Commands and paths support `{placeholder}` substitution:

`{root} {revision} {short_revision} {branch} {project} {version} {run_id}
{logs} {reports} {artifacts} {state} {tmp} {source_date_epoch} {python}
{venv_python} {release_dir}`

`{python}` resolves to the repository's interpreter (`.venv` first, then the
running interpreter's venv, then `python3`/`python` on `PATH`) and should be
used in every generated or hand-written tool invocation — a bare `python`
silently picks whatever is on `PATH`. Use `${NAME}` for environment variables
(from `[stages.*].env` or `[deploy.*].env`).

## `[project]`

| key | type | notes |
| --- | --- | --- |
| `name` | string | must start with a letter; defaults to directory name |
| `version` | string | or use `version_file` |
| `version_file` | path | e.g. `VERSION`; validated against `version_format` |
| `version_format` | `semver` \| `pep440` | |
| `description` | string | |

## `[policy]`

| key | default | meaning |
| --- | --- | --- |
| `profile` | `standard` | sets the minimum required stage set |
| `fail_fast` | `true` | skip remaining stages after a blocking failure |
| `require_clean_worktree` | `"release"` | `"always"` fails any run on a dirty tree; `"off"` never checks |
| `keep_runs` | `20` | result documents retained before pruning |
| `required_stages` | `[]` | extra stages promoted to BLOCKING |
| `require_test_discovery` | `true` | a configured test command with zero matching test files fails |
| `audit_required` | profile-based | strict/release insist on an audit command |
| `allow_no_git` | `false` | must be set explicitly to run outside a repository |
| `max_tracked_file_mib` | `5.0` | tracked-file size ceiling |

## `[repository]`

`required_paths`, `required_lockfiles` (must exist **and** be tracked),
`forbidden_paths` (glob patterns for tracked content), `check_links`
(`"off"`/`"warn"`/`"fail"`), `link_globs`, `link_ignore`, `check_conflict_markers`,
`check_license`, `max_file_bytes`, plus `[[repository.versions]]` entries:

```toml
[[repository.versions]]
path = "package.json"
format = "json"           # text | json | toml | regex
key = "version"           # dotted key for json/toml
# regex = '...'           # for format = "regex" (first capture group)
```

Every source must match `[project]`'s resolved version, or the gate fails.

## `[stages.<name>]`

Names are fixed: `format lint typecheck unit integration contract e2e
coverage security audit build package artifact repository`. Common keys:
`command` or `commands` (mutually exclusive), `fix_command(s)`, `required`,
`enabled`, `timeout` (seconds), `cwd`, `env`.

Stage-specific keys (validated per stage):

- test stages (`unit integration contract e2e`): `junit_path`, `min_test_files`,
  `min_tests`, `allow_empty`, `test_globs`. Patterns are path-aware:
  `**` matches zero or more directories (e.g. `tests/**/*.py` matches
  `tests/test_a.py`), `*` never crosses `/`.
- `coverage`: `report_path` (coverage.py XML/JSON, lcov, or a TOTAL line),
  `min_percent`.
- `build`: `outputs` (paths that must exist after the command).
- `package`: `strategy` (`archive`), `inputs`, `strip_prefix`, `output`,
  `exclude`, `format` (`tar.gz`/`tar`/`zip`).
- `artifact`: `verify_archives`, `scan_secrets`, `must_contain`, `allowlist`,
  `smoke_commands`.
- `security`: `secret_scan`, `scan_untracked`, `scan_config`, `allowlist`,
  or bring your own SAST via `command(s)`.

## `[release]`

`require_clean` (default true — a release is only taken from a committed,
reproducible tree), `require_notes`, `changelog`, `tag_prefix`, `output_dir`.

## `[deploy.<env>]`

`strategy` (`process` | `static`), `start_command` (required for `process`),
`server_command` (optional static file server), `public_dir`, `app_subdir`,
`host`, `port` (`0` = allocate a free port), `health_path`, `readiness_path`,
`health_expect`, `health_timeout`, `smoke_path`, `smoke_expect`,
`smoke_commands`, `stop_timeout`, `artifact_name`, `auto_rollback` (default
true), `env`, `env_required`.

`env` values may be `${VAR}` references. **Literal secret-shaped values are
rejected at load time** — the configuration must never contain credentials;
export them in the shell that runs `./ci deploy` and list them in
`env_required` for a clear pre-flight error when missing.

## `[hooks]`

`pre_commit_stages` and `pre_push_stages` — the stages each hook runs.
