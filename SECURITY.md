# Security policy

ci-fabric is a local-first CI/CD orchestrator with no runtime dependencies
and no network presence. Its security model — what it defends against, what
it cannot, and how the pipeline enforces its own hygiene — is documented in
[docs/security.md](docs/security.md).

## Reporting a vulnerability

This repository currently has no remote or issue tracker; it is developed
locally. Report vulnerabilities directly to the owner (Sajan Gurung) by
whatever private channel you have. Include:

1. the affected component and version (`./ci version`),
2. a reproduction or the conditions required,
3. impact, and whether the standard gates fail to catch it.

The threat model in [docs/security.md](docs/security.md) is the honest
boundary of the tool: ci-fabric enforces integrity inside a repository's
trust boundary (no tracked secrets, verified release bytes, signature-checked
deploys, traversal-safe extraction). It is not a secure-build enclave, and
`ci.toml` is code — review it like code.

## What the pipeline enforces on itself

- Tracked-content secret scanning on every run (this rule caught real
  credential-shaped content in fixtures during development).
- Dependency audit gating the pipeline: during self-CI the audit stage
  caught PYSEC-2026-1845 and PYSEC-2026-3447 in the pinned toolchain; both
  were fixed and re-verified before release.
- DTD/entity-rejecting XML report parsing (entity-expansion payloads are
  refused before any standard-library parser is invoked — proven by test
  against a malicious payload).
- No secrets in configuration: literal credential-shaped values in
  `[deploy.*].env` are rejected at load time; `${VAR}` + `env_required` is
  the only supported shape.
