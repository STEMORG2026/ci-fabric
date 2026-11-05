# Independent audit — ci-fabric

Date: **2026-11-05**
Scope: full repository at `40bd74b` (`main`, version 0.13.0), branch `arena/d84ce058-ci-fabric`.
Auditor: independent execution-based review (Arena agent), measured against
current (November 2026) Python packaging, CI/CD supply-chain and OSS
repository standards. This is a second pass over the same tree reviewed in
[audit-report.md](audit-report.md); it verifies that report's claims and
searches for what it missed.

Companion execution plan: [plans/audit-remediation-2026-11.md](plans/audit-remediation-2026-11.md).

---

## 1. Executive summary

ci-fabric is a genuinely well-engineered local CI orchestrator: strict
configuration, honest gate semantics, deterministic packaging, real SBOM and
provenance, four test tiers with 256 tests, and unusually candid documentation.
The previous audit's core claims hold up. **However, "verified green" was
verified on one machine class, and this audit broke it on two more:**

| # | Severity | Finding | Verified by |
| --- | --- | --- | --- |
| CF-01 | **HIGH** | Test suite fails on Python 3.11.0–3.11.3, which the project claims to support (`requires-python = ">=3.11"`) | Full pipeline run on 3.11.2 |
| CF-02 | **MEDIUM** | `scripts/bootstrap.sh` fails on a fresh venv with system pip 23.0.1 (Debian bookworm) — the pinned pip is never installed first | Clean bootstrap reproduction |
| CF-03 | **MEDIUM** | Build/package cache restore accepts `../` paths from the cache manifest → arbitrary file write / directory delete outside the repo | Working proof-of-concept |
| CF-05 | **MEDIUM** | GitHub Actions pinned 2–3 majors behind (v4/v5 vs current v7); node20-based action runtimes are being retired by GitHub | GitHub API, live |
| CF-06 | **MEDIUM** | Hosted CI tests only Python 3.11; classifiers claim 3.11–3.14; Python 3.15 shipped Oct 2026 and is absent everywhere | PyPI wheel tags, workflow read |
| CF-07 | **MEDIUM** | Hosted releases are unsigned (no signer configured) although cosign-keyless is implemented and `id-token: write` is granted; no GitHub Release is published; tag↔version match is never checked | Workflow + ci.toml + release.py read |
| CF-04, CF-08…CF-29 | LOW / INFO | Toolchain refreshes, doc drift (7 files), metadata placeholders, governance gaps, god files, minor correctness | See register below |

**Verdict:** no remotely exploitable vulnerability; one local-integrity
hardening gap with a PoC (CF-03); two reproducibility defects that break the
project's own "green on any supported machine" promise (CF-01, CF-02); and a
supply-chain freshness debt that is exactly what SHA pinning without
automation produces (CF-05). All are fixable in days, not weeks. The
remediation plan sequences them so the pipeline stays green at every step.

Scorecard (this audit's rubric, 10 areas):

| Area | Grade | Note |
| --- | --- | --- |
| Gate model & fail-closed design | A | ERROR≠FAIL, discovery gates, suspensions on record |
| Test discipline | A− | 256 real tests, failure-path first; one env-sensitive test (CF-01) |
| Reproducibility | B− | Deterministic archives ✓, but bootstrap + oldest-Python break (CF-01/02) |
| Supply chain | B | Hash-pinned dev lock, SBOM, provenance ✓; stale actions, unsigned releases (CF-05/07) |
| Security hardening | B | Traversal-safe extraction everywhere except the cache (CF-03) |
| Hosted CI adapter | B− | Thin and correct, but single-version, stale pins (CF-05/06) |
| Documentation honesty | A− | Rare quality; 7 files carry stale facts (CF-12…CF-18) |
| Packaging metadata | C+ | Placeholder homepage, PEP 639 floor, missing URLs (CF-19) |
| OSS governance | B− | Public repo, but SECURITY.md denies it exists; no templates/Dependabot (CF-20/21) |
| Architecture | B | Cohesive but 4 god files; plan exists, counts stale (CF-22) |

---

## 2. Method and evidence

Everything below was executed, not assumed, in a clean sandbox (Debian,
Python 3.11.2, network to GitHub/PyPI only):

1. **Clean bootstrap** — `./scripts/bootstrap.sh` → **FAILED** in
   `--require-hashes` mode (CF-02). Root-caused; verified the fix (install the
   hash-pinned `pip==26.2.2` first, then the lock installs cleanly).
2. **Doctor** — `./ci doctor` → OK, 19 checks.
3. **Full pipeline** — `./ci ci` → **FAIL**: `unit` tier,
   `tests/unit/test_oci_image.py::test_the_archive_unpacks_into_the_layout`,
   `TypeError: TarFile.extractall() got an unexpected keyword argument
   'filter'` (CF-01). 255/256 tests pass otherwise.
4. **Full pipeline with a temporary 4-line compat shim** (reverted after
   measurement) → **PASS/WARN**: 55 gates pass, 0 fail; coverage gate
   **80.48%** line-rate vs 74% floor; build/package/artifact green; the only
   WARNs were the dirty-tree notices caused by the shim itself.
5. **Individual stages** — `./ci security` (secret scan + bandit) PASS;
   `pip-audit -r requirements-dev.lock` → **no known vulnerabilities**;
   `./ci build` (build+package+artifact) PASS.
6. **Test inventory** — `pytest --collect-only` → **256** tests
   (unit 144, integration 87, contract 18, e2e 7). Docs claim 252 and
   "unit (120) / integration (71)" (CF-12/13).
7. **Live GitHub API** — verified all three pinned action SHAs exist and are
   the claimed majors; fetched current releases: checkout **v7.0.1**,
   setup-python **v7.1.0**, upload-artifact **v7.0.2** (CF-05). Repo
   metadata: public, issues enabled, **0 tags, 0 releases**, no topics,
   no homepage (CF-20/23).
8. **Live PyPI** — latest versions of every dev tool (CF-09); `cp315` wheels
   exist for coverage 7.17.2 → Python 3.15 is released (CF-06).
9. **Cache-traversal PoC** — crafted a `cache.json` whose output path is
   `../victim.txt`; `buildcache.lookup()` accepted it and
   `buildcache.restore()` **overwrote a file outside the repository root**
   (CF-03). PoC kept out of the repo; regression test specified in the plan.
10. **Static review** — all 30 source modules and the test tiers read or
    spot-audited; every claim in `docs/audit-report.md` cross-checked
    (section 5).

---

## 3. What is genuinely good (verified, keep as-is)

- **Fail-closed configuration**: unknown keys are hard errors; schema version
  enforced; literal credential-shaped deploy env values rejected at load;
  `${VAR}` expansion fails on missing variables, never silently empties.
- **Gate semantics**: `ERROR` (unverified) is distinct from `FAIL` and blocks
  identically; zero-test discovery fails; suspensions require a reason and
  are echoed in every run.
- **Deterministic supply chain**: sorted entries, `SOURCE_DATE_EPOCH` mtimes,
  SHA256SUMS re-verified after signing and before deploy; CycloneDX SBOM from
  real lockfile parses (uv/poetry/requirements/npm/pnpm/yarn/Cargo/Go);
  in-toto-shaped provenance; daemonless deterministic OCI layouts.
- **Traversal-safe archive extraction** (`util.extract_archive` rejects links
  and `..`), DTD/entity-rejecting XML parsing, redacted secret-scan contexts,
  process-group timeout cleanup, atomic writes with correct
  `except BaseException` cleanup-and-reraise.
- **Signing contract**: BYO signer + cosign-keyless both fail closed; a signer
  that rewrites checksummed content is rejected; deploy refuses unverifiable
  bundles. (The gap is that this repo doesn't *use* it — CF-07.)
- **Docs drift gates**: `docs/reference.md` regenerated and diff-gated every
  run; version cross-check VERSION↔pyproject; link gate; changelog-structure
  gate. These caught nothing because the generated docs are in fact in sync.
- **Dev toolchain**: hash-pinned lock, `--require-hashes` bootstrap,
  `pip-audit` clean as of today, mypy strict, ruff, `filterwarnings=error`.
- **Honest limitations**: raw placeholder interpolation, pattern-based secret
  scanning, non-hermetic local runs, POSIX-only — all documented accurately,
  with `{name}_quoted` mitigations present in code (verified).

---

## 4. Finding register

Severity: **HIGH** = breaks a supported configuration or promise today;
**MEDIUM** = real risk/debt that standards now require addressing;
**LOW** = should fix, bounded impact; **INFO** = observation / optional.

### Correctness

**CF-01 — HIGH — Suite red on Python 3.11.0–3.11.3.**
`tests/unit/test_oci_image.py:91` calls `tar.extractall(unpacked,
filter="data")`. The `filter` parameter exists only from 3.12 / 3.11.4
(backport). `pyproject.toml` declares `requires-python = ">=3.11"`,
`stages/precheck.py` accepts any 3.11.x, and `docs/adoption.md` promises
"3.11+". On Debian bookworm's 3.11.2 the full pipeline fails. The hosted CI
masks this because `setup-python` ships a newer patch.
*Fix:* version-guard the test (or extract via `ci_fabric.util.extract_archive`,
which is the project's own safe extractor), and add an oldest-supported-Python
CI job so this class of drift is caught. A compat shim was verified to make
the full pipeline green on 3.11.2.

**CF-02 — MEDIUM — Bootstrap fails on fresh system pip.**
`bootstrap.sh` installs the whole hash-pinned lock with whatever pip created
the venv. On pip 23.0.1, `--require-hashes` rejects the uv-compiled lock
because uv strips extras: the lock pins `coverage==7.17.0` while
`pytest-cov==6.3.0` requires `coverage[toml]>=7.5`, and old pip treats the
extra as an unpinned requirement (it even tried to fetch coverage 7.17.2 —
a *different version than the lock*, which is exactly what hash mode exists
to prevent). The lock pins `pip==26.2.2`, but it is installed in the same
transaction, too late to matter. Verified: installing the pinned pip first
(hash-verified) makes the identical lock install cleanly.
*Fix:* bootstrap installs the pinned pip first, then the lock; and/or
regenerate the lock with `uv pip compile --no-strip-extras`. Document both.

**CF-03 — MEDIUM — Cache restore is a traversal primitive (PoC).**
`buildcache.lookup()` validates digests but not path shape;
`buildcache.restore()` joins manifest-supplied `path` values onto
`context.root` and `shutil.rmtree`s directory destinations. A manifest entry
`{"path": "../victim.txt", "sha256": <matching>}` passes lookup (the source
resolves inside the cache tree) and writes **outside the repository** at
restore; a directory-valued path deletes outside the repository. Exploit
requires local write access to `.localci/cache/` (opt-in feature), so this is
defense-in-depth — but it contradicts the project's own traversal-safe
doctrine enforced everywhere else, and cache dirs get synced, backed up and
shared. *Fix:* `safe_relative_path()` + `is_within()` on both source and
destination in `lookup`/`restore`; refuse to delete tracked paths; regression
test from the PoC.

**CF-04 — LOW — `version_key()` misorders prereleases.**
`util.version_key("1.0.0-rc1") == (1,0,0,1)` sorts *after* `(1,0,0)`, so
`list_releases()`/latest-fallback can prefer a release candidate over the
final release. Semver requires the opposite. *Fix:* prerelease-aware ordering
+ unit test.

### Supply chain & hosted CI

**CF-05 — MEDIUM — Actions 2–3 majors stale; node20 retirement.**
Pins (all valid SHAs, verified): checkout `11d5960a…` (v4 line),
setup-python `a26af69b…` (v5 line), upload-artifact `ea165f8d…` (v4 line).
Current: **checkout v7.0.1, setup-python v7.1.0, upload-artifact v7.0.2**.
The v4/v5 generations run on node20, which GitHub is retiring in favor of
node24; these pins will start failing or warning on hosted runners. SHA
pinning without an update bot is how pins rot — the SUP-004 suppression
("avoid surprise PRs") does not hold for actions, where *no* PR means silent
decay. *Fix:* bump to the v7 SHAs (listed in the plan) and add Dependabot for
`github-actions` only, monthly, grouped.

**CF-06 — MEDIUM — Single-version CI vs a 4-version support claim.**
`ci.yml` tests 3.11 only. Classifiers list 3.11–3.14; Python 3.15 released
Oct 2026 (cp315 wheels live on PyPI) and appears nowhere. CF-01 is the direct
consequence: the oldest supported patch level is never exercised.
*Fix:* matrix over 3.11–3.15 (oldest + newest at minimum), add the 3.15
classifier once green, keep the canonical full pipeline on one version and
run the test tiers across the matrix.

**CF-07 — MEDIUM — Releases unsigned and unpublished; tag not cross-checked.**
`release.yml` grants `id-token: write` + `contents: write`, but `ci.toml
[release]` configures **no signer**, so every hosted bundle is unsigned even
though cosign-keyless is implemented and the OIDC permission is already
granted. The workflow only uploads a 90-day artifact — no GitHub Release, no
detached signature, no SLSA attestation. Nothing checks that the pushed tag
(`v*`) matches the resolved `VERSION`: a `v9.9.9` tag on a 0.13.0 tree silently
produces a 0.13.0 bundle. The repo has **0 tags / 0 releases** despite 13
versions of release notes. *Fix:* enable `signer = "cosign-keyless"` with the
workflow identity, install cosign in the workflow, verify tag↔version,
publish a GitHub Release with bundle + SHA256SUMS + sigstore bundle; cut
`v0.13.0`. Optionally add `actions/attest-build-provenance`.

**CF-08 — LOW — Workflow hardening details.**
`persist-credentials: false` is not set on checkout (the token lingers in
`.git/config` for the job); release permissions are workflow-level rather
than job-level; no `workflow_dispatch` for manual runs; no environment
protection on the release job. *Fix:* all four, small diffs.

**CF-09 — LOW — Dev toolchain refresh available.**
Lock → latest (PyPI, today): ruff 0.17.2→**0.17.3**, mypy 1.20.2→**2.4.1**
(major; `requirements-dev.in` caps `<2`), pytest 9.1.1→**9.2.0**, coverage
7.17.0→**7.17.2**, pytest-cov 6.3.0→**7.1.0** (major; caps `<7`). bandit,
pip-audit, build, setuptools, wheel are current. `pip-audit` on the current
lock: **clean**. *Fix:* take patch/minor bumps now; evaluate mypy 2 and
pytest-cov 7 deliberately (read changelogs, run strict); regenerate the lock
with `--no-strip-extras` (ties into CF-02).

**CF-10 — LOW — Build-system metadata lag.**
`build-system.requires = ["setuptools>=68", "wheel"]`: the SPDX
`license = "MIT"` field is PEP 639, which needs setuptools **≥77** to be
interpreted correctly (older setuptools treats it as deprecated free text);
`wheel` has been unnecessary since setuptools 70.1 vendored it. No
`license-files`, no `[project.urls]` beyond a placeholder (CF-19).
*Fix:* `requires = ["setuptools>=77"]`, add `license-files = ["LICENSE"]`,
verify `python -m build` both isolated and `--no-isolation`.

**CF-11 — INFO — SBOM spec version.** CycloneDX 1.5 is emitted; 1.6/1.7 are
current. Optional bump after checking consumer compatibility; the honest
`SBOM_VERSION` constant makes this a one-line change plus tests.

### Documentation accuracy (the project's own "docs rot is a build failure" standard)

**CF-12 — LOW — `docs/testing.md` tier counts stale:** claims unit (120),
integration (71); actual **144 / 87** (256 total). Also "80.1% line rate" vs
measured **80.48%** (close, but the file's own standard is exactness).

**CF-13 — LOW — "252 tests" in three files:** `docs/roadmap.md`,
`docs/decisions.md` (D22), `docs/plans/god-file-refactor.md`. Actual: 256.

**CF-14 — LOW — God-file plan understates the largest file ~2×:**
`docs/plans/god-file-refactor.md` says deployment.py ~900 lines; actual
**1943**. config.py ~900→**1246**, cli.py ~1000→**1093**, supplychain.py
~900→**909**. The split plan should be re-sized before execution.

**CF-15 — LOW — ADR D9 is superseded but not marked:** "No `.github/`
workflows ship in this phase" — workflows shipped in 0.13.0. Mark D9
superseded (by the 0.13.0 adapter decision) instead of leaving it asserted.

**CF-16 — LOW — Release workflow prose drift:** `RELEASE_NOTES.md` (0.13.0)
and `docs/operations.md` both say release.yml runs `./ci release --clean-env
--tag`; the fixed workflow (HEAD commit) runs `--clean-env release` with no
`--tag`. Operations doc must be corrected; release notes should get a
dated correction line rather than a silent rewrite.

**CF-17 — LOW — `autodoc.toml` phase comment stale:** "hosted-CI adapter
deliberately deferred; no stage parallelism yet" — both shipped (0.13.0 /
0.4.0). The declaration's `phase_declared` date predates them; refresh the
comment and date.

**CF-18 — LOW — Previous audit references a script that does not exist:**
`docs/audit-report.md` claims pinning was done with `pin-actions.mjs` and
"`--check` passes"; no such file is in the repo. Either ship the checker
(e.g. `scripts/check-action-pins.sh`) or correct the claim. A pin checker is
worth having regardless (CF-05).

**CF-19 — LOW — Package metadata placeholders:**
`Homepage = "https://example.invalid/ci-fabric"`; no Repository/Issues/
Changelog URLs; GitHub repo has no homepage, no topics. *Fix:* point at
`https://github.com/STEMORG2026/ci-fabric`, add the standard URL set, set
repo description/topics.

### Governance & process

**CF-20 — LOW — `SECURITY.md` denies the repo's own hosting:** "no remote or
issue tracker; developed locally" — the repo is **public on GitHub with
issues enabled** (verified via API). "Report by whatever private channel you
have" is not a vulnerability-disclosure process. *Fix:* enable GitHub private
vulnerability reporting, reference it in SECURITY.md, give CODE_OF_CONDUCT.md
a concrete contact.

**CF-21 — LOW — Missing OSS scaffolding for a public repo:** no Dependabot
config (see CF-05), no issue/PR templates, no CODEOWNERS (suppressed as
single-maintainer — acceptable, revisit if contributors arrive), branch
protection unverifiable from here (API 403) — **verify in repo settings**:
require PR + the `ci` check on `main`, no force-push.

**CF-22 — LOW — God files (tracked, deferred):** deployment.py 1943,
config.py 1246, cli.py 1093, supplychain.py 909. The deferral (D22) is
reasonable; execute the plan *after* the correctness/security fixes, one
split per commit, pipeline green each time. Re-size per CF-14.

**CF-23 — LOW — No release exists:** 0 tags, 0 GitHub Releases for a 0.13.0
tool with 13 versions of notes. Cut `v0.13.0` through the (fixed) release
workflow so the adapter is exercised end-to-end at least once.

**CF-24 — LOW — `tests/unit/__init__.py` missing** while every other tier has
one. No current collision, but pytest's prepend import mode makes same-named
test files across tiers a future footgun; consistency is one empty file.

**CF-25 — LOW — Coverage floor 74% vs measured 80.48%.** For a tool whose
product is "trust my green", a floor ~6.5 points below reality invites silent
erosion. Raise to **78–80** (policy change, reviewed, per the project's own
"thresholds are policy" rule). Note: cli.py's 20% in a naive run is *by
design* — contract/e2e instrument CLI subprocesses via `CI_FABRIC_COVERAGE`;
the true combined figure is 80.48%.

**CF-26 — INFO — Hardcoded `LOCALCI_VERSION = "0.1.0"` and
`USER_AGENT = "ci-fabric/0.1.0"`** while the tool is 0.13.0. If these are
contract/schema versions, document them as such; if tool versions, derive
from `__version__`.

**CF-27 — INFO — `supplychain.py` docstring says "Not implemented: signing"**
— signing *is* implemented (in `release.py`). Clarify the module-boundary
wording so the honest-limits doc doesn't read as a contradiction.

**CF-28 — INFO — Trailing newlines missing** in `.python-version` and
`.editorconfig` — the latter's own rule is `insert_final_newline = true`.

**CF-29 — INFO — Optional lint expansion:** ruff select is solid
(E,F,W,I,UP,B,C4,SIM,PIE,PERF,RUF). Consider `PT` (pytest style), `S`
(bandit-in-ruff) and `T20` (print) later; opinionated, low priority.

---

## 5. Cross-check of the previous audit (`docs/audit-report.md`)

| Claim | Verdict |
| --- | --- |
| Actions pinned to commit SHAs | **True** — all three SHAs exist and match the claimed majors. But "pinned" ≠ "current": 2–3 majors stale (CF-05), and `pin-actions.mjs` is not in the repo (CF-18). |
| `id-token: write` added for cosign | **True** — but unused: no signer configured (CF-07). |
| Concurrency, timeouts, least privilege | **True** for ci.yml; release permissions could be job-scoped (CF-08). |
| `{name}_quoted` variants exist | **True** — verified in `context.py`; dogfood ci.toml interpolates no untrusted values. |
| 9/10 internet best-practice items satisfied | **Overstated** — bootstrap reproducibility (CF-02), oldest-supported Python (CF-01), action freshness (CF-05) and release signing-in-practice (CF-07) were not exercised. |
| USA 96.3/100 after suppressions | Not reproducible here (USA not installed); the suppression file is reasonable and each reason was spot-checked against source. |
| "No critical or high-severity exploitable finding remains" | Holds for *remote* exploitability. CF-03 is a local-integrity gap with a PoC; CF-01 is a high-severity correctness break on a supported interpreter. |
| God files deferred as architectural | Agreed — but the plan's line counts are stale (CF-14). |

The previous audit was honest about what it knew; its blind spot was
**execution breadth** (one Python patch level, one pip generation, no
adversarial cache input) and **freshness** (pins verified to exist, not to be
current).

## 6. November 2026 standards checklist

| Standard practice | Status |
| --- | --- |
| Actions pinned to full SHAs | ✅ (stale majors — CF-05) |
| Automated pin updates (Dependabot/Renovate) | ❌ CF-05/21 |
| Least-privilege `GITHUB_TOKEN` | ✅ ci / ⚠️ release job-scope (CF-08) |
| `persist-credentials: false` | ❌ CF-08 |
| Concurrency groups + job timeouts | ✅ |
| Test matrix across supported Pythons (incl. oldest & newest) | ❌ CF-06 |
| Hash-pinned, reproducible dev environment | ⚠️ works only with new pip (CF-02) |
| Dependency vulnerability audit in CI | ✅ pip-audit, clean today |
| SBOM in release artifacts | ✅ CycloneDX 1.5 (1.6/1.7 optional, CF-11) |
| Signed releases / provenance attestation | ⚠️ implemented, not enabled (CF-07) |
| SLSA-style provenance document | ✅ in-bundle, honestly labeled |
| Secret scanning | ✅ pattern-based, baseline-gated; history scan optional |
| SAST | ✅ bandit with reasoned suppressions |
| Coverage gate | ✅ 74% floor / 80.48% measured (raise floor, CF-25) |
| Strict typing + lint + format gates | ✅ mypy strict / ruff |
| Generated-docs drift gate | ✅ |
| SECURITY.md with a real disclosure channel | ❌ CF-20 |
| License + SPDX metadata (PEP 639) | ⚠️ floor too low (CF-10) |
| Real project URLs / repo metadata | ❌ CF-19 |
| Branch protection on default branch | ❓ verify in settings (CF-21) |
| Tagged, published releases | ❌ CF-23 |

---

*All findings above were reproduced or read from source on 2026-11-05. The
temporary test shim used for measurement was reverted; the working tree
contains only this report and its companion plan. No fix has been executed
yet — that is the plan's job, pending approval.*
