# Test strategy

ci-fabric is a CI/CD orchestrator: when it says "pass", people merge on it.
Its test suite is therefore built to fail loudly on the exact failure modes
that make CI dangerous — silent passes, misattributed failures, corrupted
state — rather than to maximize a coverage number.

## The four tiers

| tier | what it proves | examples |
| --- | --- | --- |
| **unit** (148) | every component's contract in isolation | glob semantics, archive determinism, secret-scanner allowlists, config schema rejection of every malformed input |
| **integration** (104) | stages compose and enforce policy on real fixture repositories | fail-fast skips later stages with reasons; discovery blocks a zero-test run; tampered artifacts fail integrity; release refuses dirty trees; deploy auto-rolls back a genuinely broken release to a genuinely running previous one |
| **contract** (18) | the public shapes other tools depend on are pinned | result-JSON key sets at every level, exit codes 0/1/2/130 via real subprocesses, `--json` output is pure JSON, policy document, generated reference vs the live parser |
| **e2e** (7) | the real CLI, as a subprocess, exactly as consumers run it | `init` scaffolds a loadable config; a committed secret fails `security` then passes after removal; release→deploy→health→undeploy; the `./ci` shim resolves the checkout |

Integration and e2e tests build real git repositories in temp directories and
run real commands — no mocks of git, pytest, or HTTP. The deploy tests start
actual local servers and kill them.

## Principles

1. **Test the failure paths first.** A green pipeline is easy to test; the
   product's value is what it does when red. Controlled failures are
   exercised on the self-repo every cycle (a real format-drift failure once
   skipped all 13 downstream stages with reasons — the intended behavior,
   verified end to end, then fixed via `./ci format --fix`).
2. **A silent pass is the worst outcome.** Discovery gates fail a configured
   test command that matches zero test files; a command that exits 0 but
   prints "no tests ran" fails; coverage below threshold fails with the
   measured number.
3. **Dogfood everything.** The pipeline that runs ci-fabric's tests *is*
   ci-fabric; the gates described in `gates.md` are the gates that gate this
   repository's own development.
4. **Evidence in the result document.** Every test tier asserts on the
   structured result (`.ci/results/latest.json`), not on stdout text, so the
   tests pin the machine contract, not the prose.

## Coverage

Branch coverage is measured across the full suite with coverage data
collected inside the tiers (see decision D6/D7 in
[decisions.md](decisions.md)); the CLI is measured through deliberately
instrumented subprocesses. The floor in this repository is set in `ci.toml`
(`min_percent`) — currently 78% against a measured 80.64% line rate (full
suite, 277 tests: 148 unit / 104 integration / 18 contract / 7 e2e), a small
honest buffer, never a target to game.

## Running

```sh
.venv/bin/python -m pytest tests -q          # everything (~75s)
.venv/bin/python -m pytest tests/unit -q     # fast loop (~10s)
./ci ci                                      # the pipeline that gates this repo
```

New gate behavior needs a test that watches it fail first (planted defect),
then pass — the same discipline MACP uses for its seal, for the same reason.
