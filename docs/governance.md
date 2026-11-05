# Repository governance

How this repository is protected, how contributions arrive, and how security
reports are handled. The in-repo files are authoritative for contribution
rules; the hosted settings below must be applied by a maintainer with
repository-administration permission.

## Contribution path

1. Open an issue (bug or feature form) or comment on an existing one.
2. Branch, make one logical change per commit, and follow
   [CONTRIBUTING.md](../CONTRIBUTING.md). Behavior changes need a regression
   test watched fail first.
3. Open a pull request against `main`. The PR template asks for the
   verification commands and the docs that moved with the change.
4. The hosted matrix must pass: `ci (python 3.11)` through
   `ci (python 3.15)`. See [operations.md](operations.md) for the adapter.
5. A maintainer merges. Releases are cut from `main` by a `vX.Y.Z` tag; the
   release workflow refuses a tag that does not match `VERSION`.

## Security reporting

[SECURITY.md](../SECURITY.md) is the policy. Preferred channel: GitHub private
vulnerability reporting (**Security → Report a vulnerability**). Fallback:
email `sajan.plg@gmail.com`. Conduct concerns use the same address with
`[ci-fabric conduct]` (see [CODE_OF_CONDUCT.md](../CODE_OF_CONDUCT.md)).

## Hosted settings (maintainer checklist)

The automation token used during the 2026-11 audit could read repository
metadata and rulesets but received HTTP 403 when writing repository settings,
rulesets, or private vulnerability reporting. These are therefore recorded as
maintainer actions, not as completed automation:

- [ ] **Private vulnerability reporting:** Settings → Code security and
  analysis → Private vulnerability reporting → Enable. Verify with
  `gh api repos/STEMORG2026/ci-fabric/private-vulnerability-reporting`
  (expect `{"enabled": true}`).
- [ ] **Branch protection / ruleset:** apply `.github/rulesets/main.json`
  (require PR, block force-push/deletion, require the five matrix checks;
  zero required approvals so a single maintainer can merge their own green
  PR). Via API:
  `gh api -X POST repos/STEMORG2026/ci-fabric/rulesets --input .github/rulesets/main.json`
  or Settings → Rules → Rulesets → New branch ruleset. Verify with
  `gh api repos/STEMORG2026/ci-fabric/rulesets`.
- [ ] **Repository metadata:** description
  "Local-first, repository-native CI/CD orchestration for any stack.",
  homepage `https://github.com/STEMORG2026/ci-fabric#readme`, topics
  `ci cd local-first devops supply-chain python slsa sigstore cyclonedx`.
  `pyproject.toml` already points at the real GitHub URLs.

Until the ruleset is applied, `main` is protected only by convention and the
green-matrix requirement in CONTRIBUTING.md. The ruleset file is committed so
the intended protection is reviewable and reproducible.

## Release authority

Only a maintainer pushes `v*` tags. The tag gate
(`scripts/check-release-tag.sh`) and the release workflow's `--verify-tag`
mean a tag cannot publish a different version than `VERSION` names. Signing
identity is the per-tag GitHub Actions OIDC identity recorded in
`release.json`; see [operations.md](operations.md) for verification.
