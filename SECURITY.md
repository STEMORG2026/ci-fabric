# Security policy

ci-fabric is a local-first CI/CD orchestrator with no runtime dependencies and
no background network service. Configured stages may contact the network when
you ask them to (for example `pip-audit`); ci-fabric itself does not phone home.
Its security model — what it defends against, what it cannot, and how the
pipeline enforces its own hygiene — is documented in
[docs/security.md](docs/security.md).

## Reporting a vulnerability

**Preferred:** GitHub private vulnerability reporting — open the repository's
**Security** tab and choose **Report a vulnerability**. This keeps the report
private to the maintainers while it is triaged.

**Fallback (always available):** email **sajan.plg@gmail.com** with
`[ci-fabric security]` in the subject. Do **not** open a public issue for a
suspected vulnerability.

Please include:

1. the affected component and version (`./ci version`),
2. a reproduction or the conditions required,
3. impact, and whether the standard gates fail to catch it.

We aim to acknowledge within 7 days and to coordinate disclosure within 90
days (sooner for an actively exploited issue). The threat model in
[docs/security.md](docs/security.md) is the honest boundary of the tool:
ci-fabric enforces integrity inside a repository's trust boundary (no tracked
secrets, verified release bytes, signature-checked deploys, traversal-safe
extraction and cache restore). It is not a secure-build enclave, and
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
- Cache restore is containment-checked: traversal, absolute paths, symlinked
  destinations, `.git`/`.ci`/`.localci` state and tracked sources are rejected
  as corruption, never served or restored.
