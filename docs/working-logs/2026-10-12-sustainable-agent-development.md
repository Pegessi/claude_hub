# Stage three: sustainable agent-led development

Date: 2026-10-12. Starting candidate:
`f2a89d22b5dd505125e7adf199144d3525183095` (`feat/quality-stage3`).

## Scope

The goal is continued maintainability of Claude Hub, not only UI acceptance or
instructions for agents using the running product. The authorized additions are
verification-environment provenance, protection against committing machine-local
configuration, and explicit review of merge decisions. They complement the
existing Task persistence/Mailbox and Task creation-flow refactoring.

These checks do not guarantee correct architecture or complete semantic merges.
No main integration, push, service restart, hook installation or scheduled agent
is included in this change. The primary checkout and its local settings remain
outside the implementation scope.

## Research and decisions

The following sources were read directly for this work. They are engineering
experience and recommendations, not a controlled evaluation of Claude Hub.

| Source | Relevant recommendation | Application here and limitation |
| --- | --- | --- |
| [OpenAI, Harness engineering](https://openai.com/index/harness-engineering/) | Keep AGENTS.md a short map; put current knowledge in the repository; enforce clear architectural invariants with tools and structural tests. | Keep the entry guide short and update the existing workflow section. Enforce only concrete boundaries that are actually defined. The article reports one internal project and explicitly says long-term architectural coherence is still being learned. Its permissive merge/automatic-cleanup practices do not override this repository's approval requirements. |
| [Google, Small CLs](https://google.github.io/eng-practices/review/developer/small-cls.html) | A change should be self-contained, include related tests, and leave a working system. Separate substantial refactoring from behavior changes. | Review and integrate independently testable slices when authorized. Keep upstream merges separate from new feature work. Smallness is conceptual, not a line-count quota. |
| [Martin Fowler, Continuous Integration](https://martinfowler.com/articles/continuousIntegration.html) | Integrate frequently, keep feedback fast, and use self-testing code to detect semantic conflicts that version control cannot see. | Merging main into a feature branch is only partial integration. Reduce long-lived divergence through approved small deliveries; rerun behavior checks after merges. Do not treat native Git reconstruction as semantic verification or automatically push to main. |
| [Software Engineering at Google, chapter 12](https://abseil.io/resources/swe-book/html/ch12.html) | Test observable behavior through the unit's public API; prefer state assertions over unnecessary interaction assertions so internal refactoring remains possible. | Preserve contract assertions when moving code. Explain changes to existing tests rather than counting them as evidence of poor design. Mock external effects, but use real local components where deterministic and affordable. Side-effect count/order assertions remain useful when they express an actual safety contract. |

### What is already useful

The shared local/CI verification entry, isolated feature runtimes, behavioral
browser gates, independent review and current Task ownership documentation are
reusable mechanisms. They should be maintained rather than replaced with another
orchestration framework. The root guide already acts as a navigation map; the
combined length of all linked documents is not the amount loaded for every task.

### Recurring development decisions

The updated [change-boundary guidance](../AGENT_WORKFLOW.md#choosing-a-change-boundary)
uses the same questions for feature work, fixes and refactoring:

1. Which existing module owns the behavior and authoritative state? Which callers
   and external contracts change? A second store or execution path needs an
   explicit ownership decision.
2. What is the smallest complete change that can be tested and reviewed? Move
   substantial responsibilities separately from new behavior where practical.
3. Does testing require unrelated workflows, or do callers mutate another owner's
   internals? These are reasons to examine a boundary. Large files alone are not.
4. Which test demonstrates the requirement, including failure or interruption
   where relevant? A guard must reject a representative bad case and accept a
   legitimate case. Keep the setup faithful to the real contract.
5. What current contract or executable protection changes after a repeated
   defect? If the repair is deferred, record the affected owner, evidence and next
   action in the existing work record. Avoid accumulating anonymous warnings.

This is review guidance, not proof that agents will always make good decisions.
The new automated checks cover narrower, explicitly described conditions.

### Deliberately not added

- No file-length, coverage-percentage or changed-test-count threshold. These
  measurements can help investigation but do not decide design quality.
- No mandatory layer-import framework. The five-layer table describes
  responsibilities of existing components, not an import dependency graph.
  A future structural rule needs a concrete forbidden dependency and a tested
  legitimate alternative before it becomes a gate.
- No automatic refactoring, cleanup, scheduled reviewers or automatic merging.
- No duplicate task/debt store, mass conversion of historical logs, or new
  provider-specific instruction hierarchy. Update the narrow current contract
  when it changes; retain historical evidence as history.
- No claim that checking mock key names alone validates API behavior. Required
  fields, types, discriminators, errors and asynchronous behavior matter too.

## Implementation

- `check_backend_environment.py` checks the actual selected interpreter, editable
  project and four tool modules/distributions. `verify.sh` uses the same isolated
  module invocation for subsequent tools; JUnit validation also uses `-I`.
- `check_checkout_safety.py` distinguishes staged blobs from a fixed commit,
  rejects direct Git snapshot-redirection variables, ignores Git replacement refs, and
  checks the two deliberately configured paths. It does not install a hook.
- `check_merge_decisions.py` reconstructs a fixed two-parent merge in a temporary
  object database and requires reasons for native conflicts and other deviations.
  The historical f2 record keeps the discovered dialog-hover omission explicit;
  the later repair does not rewrite that history into a successful merge.
- Checker scripts join format/type validation through `check_*.py` discovery.
  Runtime/CLI regression tests are in the default backend suite. A separate CI job
  checks committed configuration, while merge review stays an explicit target.
- The extracted dialog now uses the same contrast-safe hover fill as main. The
  theme test checks the inherited-light-text hover case across discovered Vue
  files; it failed on the original dialog and passes after the two-line repair.

## Final validation

Owner-run evidence root: `/tmp/claude-hub-stage3-sustainability.56cC2T`.
Counts below describe separate, overlapping scopes and must not be added together.

| Check | Result | Evidence relative to that root |
| --- | --- | --- |
| Final `verify.sh all`, timeout scale 1 | Exit 0; all nine targets pass. Backend 3176 passed, 7 skipped, 33 warnings, 230.85 s; frontend 772 tests; mypy 244 files; formatting, lint, frontend types and build pass. | `owner-release/all.log`, `owner-release/all.exit` |
| Explicit browser mocks, owned port 5297 | Exit 0; all nine business and lifecycle reports pass; context/browser/driver closed; Vite exit 0, port not accepting. | `release-checks/browser-mocks.log`; raw reports at `/tmp/claude-hub-browser-mocks.1DYqHO/run`; independent readback `browser-release-audit.json` |
| Restored-product HMR owner case | Exit 0. | `owner-release/owner-single.log`, `.exit` |
| Full HMR, timeout scale 3 | Exit 0; 12 passed, zero skipped/failures/errors in the JUnit gate, 112.87 s. | `owner-release/terminal-hmr.log`, `.exit`; JUnit `/tmp/claude-hub-verify.SpVkV8/junit.xml` |
| Terminal replay, timeout scale 3 | Exit 0; 29 passed, 167.12 s. | `replay-correct-browser.log`, `.exit` |
| Real foreign-environment rejection | The supplied existing stage-two environment is rejected before mypy, exit 1 as expected. | `foreign-environment.log`, `.exit` |
| Historical merge-decision record | Five paths covered; semantic review and later commits explicitly not certified by the tool. | `docs/working-logs/2026-10-12-stage3-merge-decisions.json` |

The seven default-suite skips are one dirty-tree Git provenance check, one optional
sidebar browser test, and five optional Goal UI tests. The Git provenance check
is rerun after the delivery commit; the other six are not claimed as executed.
Browser mocks and terminal tests do not prove real Provider/Bot/OAuth integration.
Browser lifecycle evidence retains `process_tree_exit=not_verified`.
The new GitHub job has not run remotely: this branch has not been pushed.

### Failures retained and corrected

- The first merge inspection rejected the repository's shallow history. Support
  was limited to boundaries known to precede the unique merge base. A repeated
  boundary fixture then reproduced an overly strict rejection; deduplicating the
  boundary set fixed it. Both the failure and subsequent passing tests are kept.
- Initial typing found two `SimplePath` conversions; they now convert to `str`
  before `Path`. An entry test initially counted the checker filename in mypy's
  inputs as a second probe; it now matches the full invocation, not a substring.
- Two checkout quoting tests failed when a comment supplied the expected quoted
  text. Parsing quote-preserving, comment-free tokens fixed the actual loophole.
- Independent review found Git index redirection and the unisolated final JUnit
  interpreter. A real alternate-index CLI test and contaminated-`PYTHONPATH` XML
  cases reproduced them (11 failed, 10 passed); after repair the complete safety
  and entry scopes passed 65 cases. The final full run includes these changes.
- The first explicit replay run used only the HMR browser variable. Replay uses
  Playwright's installation path, so it had 4 passes and 25 setup errors with 25
  reruns. The correct existing `PLAYWRIGHT_BROWSERS_PATH` was supplied on the next
  run; no browser was downloaded and no code was changed to accommodate that
  invocation error.

### HMR: test the owner contract, not an HTTP-count proxy

The first full HMR run stopped with 8 passes and one failure: the single-owner
case observed two history requests instead of one. Its logs do not identify the
original interleaving conclusively. A natural diagnostic rerun passed; it is not
used to dismiss that failure.

A separate, controlled experiment waited for correlation B to be consumed while
A remained active, then wrote one synthetic terminal frame while A's response was
held. The old assertion failed. The second fetch's stack was `attemptFetch` from
`check`, the existing quiet/refetch path, not a second `runRemoteStableBootstrap`.
This proves the old assertion could reject legitimate behavior; it does not
retroactively establish the first failure's cause. Evidence: `hmr-probe-1.*` and
`hmr-probe-2.*`; temporary probe source is retained outside the repository.

The final test observes every true assignment at the current unique owner-entry
flag, including true-to-true writes. It requires a ready, unchanged terminal,
waits for B's handler to finish, forces a capture-time frame, and requires both
one owner and at least two snapshot attempts. A/B each succeed exactly once,
remain non-deferred, and have no route errors. No product retry behavior changed.

Temporarily disabling the existing inflight guard made the new assertion fail
with `ownerStarts=2` after B was consumed. That negative run also had a
`Route.abort: Route is already handled` teardown error: it is recorded as one
failure plus one error, not a clean assertion-only failure. The product file was
restored byte-for-byte in `finally`; the saved source and
`hmr-owner-mutation-restoration.json` record the comparison. Normal single-case
and full-HMR runs after restoration passed without that error. The shared route
helper was not changed. The test-only observer depends on the current unique
flag assignment; changing that owner representation requires reviewing the probe.

## Review and remaining boundaries

Independent read-only review covered the fixed tool/entry/test objects and the
controlled follow-up changes, separately from the author's self-review. Object
maps are retained in `review-1`, `review-2`, and `review-3`; the final HMR test was
reviewed separately against the original f2 object and its mutation evidence.
Owner execution, static review and historical records are not interchangeable.

One auxiliary reviewer exceeded the read-only assignment by running tests and
clone experiments, then reported deleting its own temporary clones. Those runs
are excluded from acceptance evidence; their provenance is incomplete. The owner
independently reproduced the actionable findings in the task's isolated tests.
This process deviation does not disappear because the final checks passed.

There was no production restart, main merge, push, hook installation, new
background agent or dependency download. The checks protect specific contracts;
they do not replace semantic review or guarantee long-term architectural health.
