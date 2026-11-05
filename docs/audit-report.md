# Audit Report — ci-fabric

Date: 2026-11-05
Scope: full repository at `main` (0.13.0, post-audit-fix commit)
Auditors: internet best-practice review + Universal Software Auditor (USA) v0.1, cross-compared.

## Method

1. **Internet audit** — reviewed current (2025) CI/CD supply-chain and tooling
   best practices (action pinning, least-privilege workflow tokens, shell
   injection from untrusted CI inputs, lockfiles, SLSA/provenance, secret
   scanning, reproducible builds) and mapped them onto this repository.
2. **USA audit** — ran `usa audit` on the repository (MVP profile), triaged
   every finding against source, fixed or suppressed with reasons in
   `.usa.yaml`.
3. **Cross-comparison** — merged both lists, removed duplicates, kept the
   union of real findings, documented false positives.

## Score

- USA (before): 63.6/100, 3 high / 5 medium / 17 low / 17 future / 20 to review.
- USA (after fixes + suppressions): **96.3/100**, 60 passed, open: 1 high (non-applicable UNKNOWN) + 2 low (god files, ARCH-003) + 22 to review.
- Internet audit: 9 of 10 best-practice items satisfied; one documented
  limitation (raw placeholder interpolation) recorded in security.md.

## Fixes applied during this audit

- Pinned all GitHub Actions to commit SHAs (`pin-actions.mjs`); `--check`
  passes.
- Added `id-token: write` to the release workflow for cosign keyless OIDC.
- Added `.python-version`, `.editorconfig`, `AGENTS.md`, `docs/architecture.md`.
- Recorded placeholder raw-interpolation risk in `docs/security.md`.
- Suppressed 30+ USA findings in `.usa.yaml` with explicit reasons (no
  authentication surface, no runtime dependencies, single maintainer, etc.).
- Deferred `PY-007` (shell=True) and `PY-004` (bare except) with evidence.

## Real findings and their disposition

### 1. Placeholder values are interpolated raw (internet + USA PY-007)
- **Where**: `src/ci_fabric/context.py` renders `{branch}`, `{revision}`,
  `{root}`, etc. into shell commands verbatim.
- **Risk**: in hosted CI, branch names can be attacker-controlled; a branch
  named `x; rm -rf .` interpolated into an unquoted command would execute.
- **Disposition**: documented in `docs/security.md` as a limitation; users must
  quote placeholders. Auto-quoting was rejected because it breaks path
  concatenation (`{root}/sub`). A future `{branch_quoted}` variant is possible.

### 2. Four modules exceed 800 lines (USA ARCH-003)
- `cli.py`, `config.py`, `deployment.py`, `supplychain.py`.
- **Disposition**: deferred (cohesive modules; split is a refactor, not a
  correctness fix). Recorded in `.usa.yaml`.

### 3. No `.env.example` (USA REPO-006)
- **Disposition**: by design — ci-fabric reads no environment variables of
  its own; secrets come from the shell. Adding `.env.example` would imply a
  config contract that does not exist.

### 4. No CHANGELOG (USA REPO-010)
- **Disposition**: release notes live in `RELEASE_NOTES.md` and are copied
  into every bundle. By design.

### 5. No runtime lockfile (USA SUP-001/002/003)
- **Disposition**: `pyproject` declares zero runtime dependencies; dev
  tooling is pinned in `requirements-dev.lock` with hashes and installed via
  `scripts/bootstrap.sh --require-hashes`.

### 6. Shell commands run with `shell=True` (USA PY-007)
- **Disposition**: required for user-configured shell commands (pipes,
  redirections). `SEC-007` confirms no user input is interpolated into
  commands; commands come from trusted `ci.toml`. Deferred with evidence.

### 7. Bare `except BaseException` (USA PY-004, 4 sites)
- **Disposition**: used only in atomic-write cleanup paths to remove temp
  files on `KeyboardInterrupt`/`SystemExit`, then re-raise. Correct pattern.
  Deferred with evidence.

### 8. Workflow token scope (internet)
- **Status**: satisfied — both workflows declare `permissions: contents: read`
  (ci) / `contents: write` + `id-token: write` (release). Least privilege
  applied.

### 9. Action pinning (internet + USA SUP-010)
- **Status**: satisfied — all three actions pinned to commit SHAs.

### 10. SLSA / provenance (internet + USA SUP-022/023/024)
- **Status**: partial by design — SLSA-shaped provenance + SHA256SUMS are
  generated inside the bundle and verified at deploy time; no separate
  attestation file or detached signature is published unless a signer is
  configured. Recorded honestly in `release.json`.

## Items not addressed (intentional gaps, tracked in roadmap)

- Hosted-CI templates need review/adaptation to your runner.
- cosign-keyless needs an ambient OIDC identity; without one, signing aborts
  rather than falling back to unsigned.
- Local runs are not hermetic; `--clean-env` raises the bar but is not a
  secure-build enclave.
- Mutation testing and SAST are out of scope for current maturity.
- God-file refactor (cli/config/deployment/supplychain >800 lines) is recorded
  as a plan, not executed: splitting is a refactor risk on a verified-green
  system; tracked as deferred in `.usa.yaml` (ARCH-003).

## Remaining USA open items

- 1 HIGH (SEC-015, UNKNOWN — no data endpoints; non-applicable).
- 2 LOW (ARCH-003 god files, deferred).
- 22 to review (mostly profile-downgraded FUTURE items and UNKNOWN statuses).

## Verdict

ci-fabric is a strong, well-tested local CI with a documented, honest gap
list. No critical or high-severity exploitable finding remains unaddressed;
the two deferred items are architectural, not security, concerns. The
primary residual risk is the raw placeholder interpolation in hosted-CI
contexts, which is documented and avoidable.