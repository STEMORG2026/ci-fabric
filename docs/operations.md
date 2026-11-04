# Operations

## Command reference

| command | stages run |
| --- | --- |
| `./ci ci` | the full canonical pipeline |
| `./ci validate` | repository |
| `./ci check` | format, lint, typecheck, security |
| `./ci format [--fix]` | format check (or the configured fix command) |
| `./ci lint` / `typecheck` | that stage |
| `./ci test` | unit, integration, contract, e2e, coverage |
| `./ci unit` / `integration` / `contract` / `e2e` / `coverage` | that tier |
| `./ci security` | security + audit |
| `./ci audit` | dependency audit |
| `./ci build` | build, package, artifact |
| `./ci package` | package + artifact |
| `./ci artifact` | artifact validation only |
| `./ci run --stages a,b [--skip c]` | explicit selection |
| `./ci release [--version V] [--tag] [--notes-file F] [--force]` | full pipeline, then a verified local release bundle |
| `./ci deploy --env E [--version V]` | deploy a verified local release |
| `./ci rollback --env E` / `undeploy --env E` | restore the previous deployment / stop |
| `./ci health --env E` / `smoke --env E` | probe the running deployment (smoke re-proves invariants) |
| `./ci deploy-status [--env E] [--json]` | deployment state (safe to parse; `--json` output is pure JSON) |
| `./ci doctor [--json]` | environment and tooling diagnosis |
| `./ci policy [--json]` | effective policy document |
| `./ci status [--json]` | project, last run, hooks, deployments |
| `./ci releases [--json]` | verified local releases |
| `./ci hooks install\|uninstall\|status` | git hook management |
| `./ci clean [--all] [--keep N]` | prune old run state |

Exit codes: `0` success · `1` blocking gate failed · `2` configuration or
usage error · `3` missing prerequisite · `130` interrupted.

## Run state layout

Ephemeral, pruned to `keep_runs`, gitignored:

```
.ci/
  run.lock                # one mutating run at a time (flock)
  policy.json             # effective policy snapshot + config hash
  logs/<run_id>/          # full stdout+stderr of every command
  reports/<run_id>/       # summary.md, environment.json, gate reports
  artifacts/<run_id>/     # package archive, manifests, SBOM, provenance
  results/<run_id>.json   # the machine-readable result document
  results/latest.json     # same document, always current
```

Durable, also gitignored:

```
.localci/
  releases/<version>/     # release bundles (flat layout, SHA256SUMS-verified)
  cache/build/<key>/      # content-addressed build cache (opt-in, per stage.cache)
  deploy/<env>/           # current.json, previous.json, last-failure.json,
                          # history.jsonl, releases/<deploy_id>/
  githooks/               # generated hook dispatchers
```

Every result document records the full gate tree with commands, exit codes,
logs, remediation hints, the policy snapshot, and the environment it ran in.
`--json` outputs never mix human text into stdout.

## Releases

`./ci release` runs the whole pipeline first; only a passing pipeline can
produce a release. With `require_clean = true` it refuses a dirty tree: a
release must name a reproducible revision. The bundle is a flat directory —
`artifact-manifest.json` (rewritten to the bundle), `SHA256SUMS`,
`RELEASE_NOTES.md`, `release.json` (version, revision, epoch, profile,
signing status), the archive, and the SBOM/provenance when present. Tags are
created only with `--tag` and are treated as immutable.

## Deployment

`deploy` resolves a **verified local release** — it never rebuilds, and the
deployed bytes' SHA-256 is recorded. `process` environments run your
`start_command` in its own process group; `static` environments serve
`public_dir`; `oci` environments wrap the same verified artifact in a
deterministic OCI image layout (decisions.md D19). Ports can be fixed or
`port = 0` for automatic allocation. All deploy state changes are serialized
per environment with `flock` and appended to `history.jsonl`.

### OCI environments (`strategy = "oci"`)

No daemon and no Docker are required to *produce* the image: the layer is a
deterministic tar of the extracted artifact, and the config, manifest, index and
`oci-layout` marker are written beside it under
`.localci/deploy/<env>/releases/<deploy_id>/image/`, plus a runtime-loadable
`image.tar`. Activation builds the image, re-verifies the whole digest chain
from `index.json` down to every blob, and only then runs your optional
`image_command` handoff. Nothing is started and no port is bound, so:

- `health` re-proves the digest chain instead of probing HTTP;
- `smoke` does the same, then runs `smoke_commands` and `invariants`;
- a deployment whose layout stops matching its recorded digest fails health,
  smoke and rollback — bytes that cannot be re-proved are never reactivated;
- rollback is a *proof*, not a rebuild: the recorded layout must still hash to
  the recorded digest.

`./ci status` and `./ci deploy-status` name each environment's `strategy` and a
`target` — the URL for `process`/`static`, and the immutable `tag@digest`
reference for `oci` (whose `base_url` is empty by design; nothing is listening).

Failure behavior:

- If the new version fails health checks and `auto_rollback = true` and a
  previous deployment was running, the previous release is restarted and the
  run fails with `rolled_back` recorded.
- If no previous deployment was running (first deploy), the failure is
  recorded without rolling back.
- `rollback` swaps current/previous explicitly and restores the outgoing
  deployment if the rollback target is also broken.

## Troubleshooting

- **"another ci-fabric run is already active"** — a stale `.ci/run.lock` from
  a killed run; verify no process is running, then remove it.
- **`{python}` "not on PATH" in doctor** — you wrote a placeholder literally
  where a path belongs, or you are reading an unrendered command; doctor checks
  commands exactly as they run.
- **Zero-test discovery failures** — intended. Add tests, fix `test_globs`,
  or set `allow_empty = true` with a documented reason.
- **A stale build cache** (suspected environment influence on artifacts) —
  delete `.localci/cache/`; the next run rebuilds and re-stores. The cache
  only ever serves builds whose revision and commands match exactly.
- **Coverage combine errors after aborted runs** — stale `.coverage*` files
  at the repository root; delete them. Keep coverage data isolated per run
  (see this repository's own `ci.toml` for the pattern).
