# Plan: audit remediation, November 2026

Status: **Phases 0–1 executed 2026-11-05; hosted matrix 3.11–3.15 green on
PR #1**; Phases 2–3 proposed, awaiting approval. Phase 0 commits: `bd35327`, `4518d10`, `7a440a4`, `a4b29ba`.
Phase 1 commits: `df2fb03`, `652b2d6`, `7c1c535`, `797a50b`, `29038a1`.
Input: [../independent-audit-2026-11-05.md](../independent-audit-2026-11-05.md) (findings CF-01…CF-29)
Branch: `arena/d84ce058-ci-fabric` (all work lands here; one logical change per commit)

## Operating rules (from this repo's own conventions)

- `./ci ci` must be green before and after **every** commit; new gate
  behavior ships with a test that watches it fail first.
- Generated docs: run `./ci docs` and commit the diff whenever commands,
  stages, exit codes or placeholders change.
- Prose docs change in the **same commit** as the behavior they describe.
- Version bumps touch `VERSION` + `pyproject.toml` + a `RELEASE_NOTES.md`
  section together (the versions gate enforces it).
- No backward-compat shims; thresholds change only through reviewed `ci.toml`
  edits.

## Phase 0 — Correctness & integrity (do first, ~1 day)

These restore the project's core promise ("green on any supported machine")
and close the only PoC'd finding.

### 0.1 CF-01 — Make the suite pass on Python 3.11.0–3.11.3 ✅ (`bd35327`)
- **Change:** `tests/unit/test_oci_image.py` — replace the bare
  `tar.extractall(..., filter="data")` with the project's own
  `ci_fabric.util.extract_archive` (already traversal-safe, no version
  dependency), or a `sys.version_info`-guarded fallback. Prefer
  `extract_archive`: it tests the code consumers actually rely on.
- **Acceptance:** full pipeline green on 3.11.2 (verified locally with a
  shim during the audit) **and** on 3.14/3.15.
- **Verify:** `./ci ci` on the sandbox's 3.11.2; matrix job (Phase 1.2) on the
  rest.
- **Effort:** S · **Risk:** low (test-only).

### 0.2 CF-02 — Bootstrap installs the pinned pip first ✅ (`4518d10`)
- **Change:** `scripts/bootstrap.sh` — extract the `pip==…` stanza from
  `requirements-dev.lock` and `pip install --require-hashes` it **before**
  the full lock install; keep the rest unchanged. Add a comment explaining
  the old-pip extras bug. Optionally also regenerate the lock with
  `uv pip compile --generate-hashes --no-strip-extras` (done properly in
  Phase 1.3) and note the uv command in CONTRIBUTING.md.
- **Acceptance:** delete `.venv`, run `./scripts/bootstrap.sh` with system
  pip 23.0.1 → success; `./ci doctor` OK.
- **Verify:** fresh-venv bootstrap on this sandbox (the exact failing case).
- **Effort:** S · **Risk:** low.

### 0.3 CF-03 — Contain cache restore paths (security hardening) ✅ (`7a440a4`)
- **Change:** `src/ci_fabric/buildcache.py` —
  - in `lookup()`: reject entries whose `path` fails `safe_relative_path()`
    or whose resolved source escapes `entry.directory / "outputs"`
    (`is_within`);
  - in `restore()`: re-validate destination `is_within(context.root)` and
    refuse to `rmtree`/overwrite any Git-tracked path (mirror the
    `build.clean` protection);
  - treat violations as a cache **miss** (corrupt cache), never an error
    that blocks the build, and record the reason in the gate details.
- **Test first:** `tests/integration/test_build_cache.py` — add the audit's
  PoC (manifest with `../victim.txt`): assert lookup returns `None` and the
  outside file is untouched; add a tracked-path deletion attempt.
- **Acceptance:** PoC neutralized; existing cache tests still green.
- **Verify:** `./ci unit && ./ci integration`, then `./ci ci`.
- **Effort:** M · **Risk:** low (opt-in feature, miss-on-invalid is safe).

### 0.4 CF-04 — Semver-aware release ordering ✅ (`a4b29ba`)
- **Change:** `src/ci_fabric/util.py::version_key` — prerelease sorts before
  the final release (e.g. key `(major, minor, patch, 0, prerelease_parts)`
  vs `(major, minor, patch, 1)`); build metadata ignored per semver.
- **Test first:** `tests/unit/test_util_gates.py` — `1.0.0-rc1 < 1.0.0`,
  `1.0.0-rc2 > 1.0.0-rc1`, `1.9.0 < 1.10.0`.
- **Effort:** S · **Risk:** low (local release listing only).

**Phase 0 exit: ✅ achieved 2026-11-05.** `./ci ci` → RESULT PASS on Python
3.11.2, clean tree: 57 gates pass, 0 fail, 0 warn; coverage 80.50% vs 74%
floor; 261 tests (5 new regression tests, each watched fail before its fix).
Fresh bootstrap from system pip 23.0.1 verified; the audit's cache-traversal
PoC re-run and neutralized (lookup returns a miss, outside file untouched).

## Phase 1 — Supply chain & hosted CI modernization (~1–2 days)

### 1.1 CF-05 — Bump GitHub Actions to current majors + Dependabot ✅ (`7c1c535`)
- **Change:** `.github/workflows/ci.yml`, `release.yml` —
  - `actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1`
  - `actions/setup-python@6d4fd22b20465ccc0f7354476588e8df7c9ca0e0 # v7.1.0`
  - `actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9 # v7.0.2`
  (SHAs resolved from the GitHub API on 2026-11-05; re-verify at execution.)
- **New:** `.github/dependabot.yml` — `package-ecosystem: github-actions`,
  monthly, grouped, commit-message prefix `ci:`. Update the SUP-004
  suppression reason in `.usa.yaml` to reflect actions-only automation.
- **New (optional but recommended):** `scripts/check-action-pins.sh` — fails
  when any `uses:` is not a 40-hex SHA; wire into `repository.generated_checks`
  or a lint command. Resolves CF-18's missing `pin-actions.mjs` claim.
- **Acceptance:** hosted `ci` workflow green on a PR.
- **Effort:** S · **Risk:** low (v7 APIs are compatible for these uses).

### 1.2 CF-06 — Python support matrix ✅ (`797a50b`)
- **Change:** `ci.yml` — strategy matrix `["3.11", "3.12", "3.13", "3.14",
  "3.15"]`; keep the canonical `./ci ci --clean-env` on 3.11 (oldest) and run
  at least the test tiers (`./ci test`) on the others; upload reports
  per-version. Add `Programming Language :: Python :: 3.15` classifier once
  green; consider a `3.11.4` note in adoption.md if Phase 0.1's fix keeps the
  3.11 floor (it does).
- **Acceptance:** matrix green incl. 3.15; classifiers match reality.
- **Effort:** S · **Risk:** low; 3.15 may surface real issues — that is the
  point; fix forward.

### 1.3 CF-09 + CF-02 — Toolchain refresh & lock regeneration ✅ (`652b2d6`)
- **Change:** `requirements-dev.in` — take ruff 0.17.3, pytest 9.2.0,
  coverage 7.17.2 now. Evaluate **mypy 2.4.1** (raise cap to `<3`) and
  **pytest-cov 7.1.0** (`<8`) in a scratch venv: run `mypy src` strict and the
  full suite; adopt only if green, otherwise record the deferral with the
  concrete error in `.usa.yaml`/roadmap.
- Regenerate: `uv pip compile --generate-hashes --no-strip-extras
  --python-version 3.11 requirements-dev.in -o requirements-dev.lock`.
- **Acceptance:** fresh bootstrap green (old pip included, per 0.2);
  `pip-audit` clean; `./ci ci` green.
- **Effort:** M · **Risk:** medium for the two majors — isolated, reversible.

### 1.4 CF-07 + CF-08 — Signed, published, tag-checked releases ✅ (`29038a1`)
- **Change:** `ci.toml [release]` — `signer = "cosign-keyless"`,
  `signing_identity = "https://github.com/STEMORG2026/ci-fabric/.github/workflows/release.yml@refs/tags/v<version>"`
  (confirm exact identity format against cosign docs at execution),
  `signing_issuer = "https://token.actions.githubusercontent.com"`.
  Local releases without OIDC will now **abort by design** — document that
  maintainers either run releases via the workflow or set `signer = "none"`
  locally with a recorded reason; decide which and write it in operations.md.
- **Change:** `release.yml` —
  - job-level `permissions: {contents: write, id-token: write}`;
  - install cosign (`sigstore/cosign-installer`, SHA-pinned);
  - **tag↔version gate:** fail when `github.ref_name` ≠ `v$(cat VERSION)`;
  - `persist-credentials: false` on both checkouts (ci.yml too);
  - publish a GitHub Release (`gh release create` or
    `softprops/action-gh-release`, SHA-pinned) attaching the bundle,
    `SHA256SUMS`, `SHA256SUMS.sigstore.json`, `sbom.cdx.json`,
    `provenance.json`;
  - optional: `actions/attest-build-provenance` for SLSA v1.0 attestation;
  - add `workflow_dispatch` to ci.yml for manual runs.
- **Acceptance:** a test tag produces a signed, verifiable GitHub Release;
  `cosign verify-blob` instructions land in operations.md.
- **Effort:** M · **Risk:** medium (OIDC identity string must be exact; the
  fail-closed signer makes mistakes loud, not silent).

### 1.5 CF-10 — Packaging metadata floor ✅ (`df2fb03`)
- **Change:** `pyproject.toml` — `build-system.requires = ["setuptools>=77"]`
  (drop `wheel`), add `license-files = ["LICENSE"]`.
- **Verify:** `python -m build` (isolated) and `./ci build` (`--no-isolation`)
  both produce valid wheels; `pip-audit` still clean.
- **Effort:** S · **Risk:** low.

**Phase 1 exit (local evidence, 2026-11-05):** full `./ci ci` green on
3.11.2 with the refreshed toolchain (mypy 2.4.1 strict, pytest 9.2.0 +
pytest-cov 7.1.0, 267 tests, pip-audit clean); fresh bootstrap from system
pip re-verified; pin gate + 4 pin tests green; both packaging builds (isolated
and not) emit PEP 639 metadata. Two code fixes were needed and landed
test-first: `--clean-env` now preserves the ambient OIDC request variables
(cosign could not sign under clean-env otherwise), and `release.json` records
the RESOLVED signing identity (env-supplied per-tag identity was previously
recorded as null). **Hosted matrix: all five jobs PASS on PR #1** (run
42018570579): 3.11 full pipeline 3m52s, 3.12 4m03s, 3.13 3m21s, 3.14 3m47s,
3.15 4m13s; Dependabot config validated. The first signed GitHub Release is
proven at the Phase 2.2 tag.

**Decisions recorded:** `actions/attest-build-provenance` deferred — the
cosign-keyless signature already covers `provenance.json` via SHA256SUMS;
add attestation only if a consumer asks for SLSA v1.0 bundle format.
Branch-protection note for Phase 2.3: the required check is now the matrix
job names (`ci (python 3.11)` … `ci (python 3.15)`), not a single `ci`.

## Phase 2 — Documentation truth, metadata, governance (~1 day)

All doc fixes land in one or two commits; the links/versions/generated gates
re-verify them.

### 2.1 CF-12…CF-18 — Doc drift sweep
- `docs/testing.md`: tier counts → 145/91/18/7 (261 after Phase 0; re-measure
  at edit time), measured coverage → the post-Phase-1 number.
- `docs/roadmap.md`, `docs/decisions.md` (D22), `docs/plans/god-file-refactor.md`:
  252 → 256; god-file line counts → 1943/1246/1093/909 (re-measure at edit).
- `docs/decisions.md`: mark **D9 superseded** by the 0.13.0 hosted-adapter
  decision (add a short D23 recording the adapter, or annotate D9 — pick one
  convention and keep it).
- `docs/operations.md`: release workflow description → actual commands
  (post-Phase-1 it will include cosign + publish; write it once, after 1.4).
- `RELEASE_NOTES.md`: append a dated correction to the 0.13.0 section
  (workflow arg-order/tag fix), do not rewrite history.
- `autodoc.toml`: refresh the phase comment + `phase_declared` date.
- `docs/audit-report.md`: correct the `pin-actions.mjs` claim to point at the
  new `scripts/check-action-pins.sh` (or note the script lived elsewhere).
- `src/ci_fabric/supplychain.py` docstring (CF-27): "signing lives in
  release.py; this module covers archives/SBOM/provenance".
- **Verify:** `./ci validate` (links, versions, generated) + full `./ci ci`.

### 2.2 CF-19 + CF-23 — Real metadata and the first release
- `pyproject.toml [project.urls]`: Homepage/Repository/Issues/Changelog →
  `https://github.com/STEMORG2026/ci-fabric` (+ `/issues`, RELEASE_NOTES.md).
- GitHub repo settings (manual or `gh api`): description, homepage, topics
  (`ci`, `cd`, `local-first`, `supply-chain`, `python`, `slsa`).
- After Phases 0–1 are merged to `main`: bump to **0.13.1** (or 0.14.0 if the
  matrix/signing changes are judged feature-level — recommend 0.14.0), write
  the notes section, tag `v0.14.0`, let the fixed release workflow publish the
  first signed GitHub Release. This exercises CF-07's whole path end-to-end.

### 2.3 CF-20 + CF-21 — Disclosure channel & repo governance
- Enable **GitHub private vulnerability reporting** (Settings → Security);
  rewrite SECURITY.md's reporting section to use it; keep the owner contact
  as fallback with a concrete address.
- CODE_OF_CONDUCT.md: add the same concrete contact.
- Add `.github/ISSUE_TEMPLATE/bug_report.md` + `feature_request.md` (minimal,
  evidence-first: version, command, result JSON, log) and
  `.github/pull_request_template.md` (checklist: `./ci ci` green, docs updated,
  tests-first for gate changes).
- **Verify branch protection** on `main` in settings: require PR + `ci`
  status check, block force-push (API returned 403 from the audit sandbox;
  this is a maintainer action).

### 2.4 CF-24, CF-25, CF-26, CF-28 — Small quality items
- Add empty `tests/unit/__init__.py`.
- `ci.toml [stages.coverage] min_percent`: 74 → **78** (headroom ~2.5 pts
  under the measured 80.48%; revisit upward as it climbs). Record rationale
  next to the value, per project convention.
- Decide `LOCALCI_VERSION`/`USER_AGENT` semantics: document as env-contract
  version **or** derive from `__version__`; update the result-contract test
  if the value changes.
- Trailing newlines: `.python-version`, `.editorconfig`.
- Regenerate `./ci docs` if any of the above touches the reference.

**Phase 2 exit:** every doc claim matches measured reality; disclosure
channel live; first signed release published; floor raised.

## Phase 3 — Architecture & optional hardening (scheduled, ~2–4 days)

### 3.1 CF-22 — Execute the god-file refactor (D22 plan, re-sized)
Order by risk, one split per commit, `./ci ci` green each:
1. `supplychain.py` (909) → `archive.py` / `sbom.py` / `provenance.py` +
   re-exports (most mechanical).
2. `cli.py` (1093) → parser/dispatch + `cli_handlers.py`.
3. `config.py` (1246) → dataclasses/load + `config_validation.py`.
4. `deployment.py` (1943) → orchestration + `deployment_health.py` +
   `deployment_rollback.py` (largest; do last, with the deploy invariant
   tests as the safety net).
Preserve the plan's invariant: all public names re-exported; no behavior
change. Update `.usa.yaml` ARCH-003 as each module drops under 800.

### 3.2 Optional, decide explicitly (record in roadmap either way)
- CF-11: CycloneDX 1.5 → 1.6 (check downstream consumers first).
- CF-29: ruff rule families `PT`, `S`, `T20` (expect a fix-up commit).
- History-aware secret scanning (gitleaks) as an *additional* security
  command — fits the BYO model; document the network/tool prerequisite.
- Mutation testing on `gates.py`/`buildcache.py`/`util.py` only (mutmut or
  cosmic-ray) — the trust-critical core, not the whole tree.

## Sequencing & dependencies

```
0.1 ─┐
0.2 ─┼─► Phase 0 exit (green on 3.11.2)
0.3 ─┤        │
0.4 ─┘        ▼
        1.1 ─► 1.2 ─► 1.3 ─► 1.4 ─► 1.5   (1.4 needs 1.1's pin discipline)
                          │
                          ▼
                   Phase 2 (docs written against post-Phase-1 reality)
                          │
                          ▼
                   2.2 release v0.14.0 (exercises 1.4 end-to-end)
                          │
                          ▼
                   Phase 3 (refactor on a verified-green, signed base)
```

## Global definition of done

- [ ] `./ci ci` green on Python 3.11.2 **and** the hosted matrix 3.11–3.15.
- [ ] Fresh bootstrap works with system pip 23.0.1 (no manual pip upgrade).
- [ ] Cache-traversal PoC is a regression test and fails closed (cache miss).
- [ ] All `uses:` are current-major SHAs; Dependabot PRs arrive monthly;
      pin-check script gates drift.
- [ ] `pip-audit` clean on the regenerated lock; mypy strict + ruff green.
- [ ] A signed GitHub Release exists, verifiable with the documented cosign
      command; tag↔version gate proven by a deliberate mismatch test.
- [ ] Every CF-12…CF-18 doc claim re-measured and corrected; `./ci validate`
      and the generated-drift gate green.
- [ ] SECURITY.md points at a live private disclosure channel; branch
      protection verified on `main`.
- [ ] Coverage floor raised and passing; god-file plan executed or
      re-deferred with updated counts and a date.

## Rollback strategy

- Every task is an independent commit on the session branch; revert granularity
  = one commit.
- Riskiest items (mypy 2 / pytest-cov 7 adoption, cosign identity, deployment
  split) are isolated commits with the prior lock/workflow/module restorable
  by `git revert` alone.
- The release workflow change is proven on a test tag before the real
  `v0.14.0`; a failed signing attempt publishes nothing (fail-closed by
  design), so rollback is "fix the identity string and re-tag".

## Effort summary

| Phase | Effort | Calendar (1 maintainer) |
| --- | --- | --- |
| 0 — correctness & integrity | ~1 day | day 1 |
| 1 — supply chain & hosted CI | ~1–2 days | days 2–3 |
| 2 — docs, metadata, governance, first release | ~1 day | day 4 |
| 3 — refactor + optional | ~2–4 days | week 2 |

Total: **~5–8 working days** to close every HIGH/MEDIUM/LOW finding; INFO
items are explicit decisions, not obligations.
