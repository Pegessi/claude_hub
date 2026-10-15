# Chat image cross-validation

## Candidate and scope

The user requested independent cross-validation of assistant image previews,
original-image quality and shared attachment LRU retention. The reviewed base is
`311653775a0476e93cd3741c0702763818964663` on `codex/chat-image-previews`.
Follow-up fixes are isolated on `codex/chat-image-cross-check`; no merge, push,
shared-service restart or deployment is part of this review.

Two independent native reviewers owned read-only backend and frontend scopes.
They retained executable probes, original failures and positive rechecks under
`/home/tiger/claude_hub_workspace/chat-image-cross-check/`. Reporter keys and
Task control stayed with the initiator.

## Findings and fixes

1. A long assistant reply could authorize an image through its persisted text
   deltas but fail to retain it before temporary-file cleanup. The completion
   summary is truncated at 4,000 characters. A bounded per-message reference
   buffer now captures complete links from persisted streaming deltas before
   fanout, including split links. It isolates run/turn/message identity, resets
   on snapshots, clears completed turns and avoids repeating old matches.
2. Source deduplication returned an existing attachment even after its configured
   age limit. Expired captures now follow normal publication and eviction,
   replace the old version and receive a new creation time. TTL still follows
   the existing save/GC policy; ordinary reads do not initiate age eviction.
3. A KeepAlive Chat could retain its Teleport image viewer and document keyboard
   handler after another Chat activated. Deactivation now dismisses the viewer
   without restoring focus into the hidden Chat; inactive panes ignore keyboard
   shortcuts. Active viewers retain forward/reverse Tab cycling and Escape.
4. Fragment/query links ending in image suffixes were promoted to local images.
   Check navigation semantics after HTML-entity decoding and before percent
   decoding. Plain, numeric and named entities stay links; `%23` and `%3F`
   remain valid filename characters. A follow-up independent probe caught the
   entity variant and verified the final fix.

## Evidence boundaries

Independent backend probes execute the actual tailer, restricted reader and
attachment store. They cover exact temporary-file grants, rejected user/shell/
remote/traversal/symlink sources, deleted-source recovery in a fresh process,
mixed upload/output count limits, concurrent deduplication, invalid replacement,
stream state isolation, TTL and publication failure cleanup.

The joint browser harness serves the actual Vue/Pinia source on owned Vite port
5294 and the actual image API/router and attachment store on owned port 18279.
Only the image API and LRU use the real backend; all other APIs are fixtures.
No agent or provider executes. Backend state and tmux identity are private. Captured current
and reported-Chat events are replayed into private stream files, preserving
source paths; the original screenshots are read without modification.

Real image API responses for the previously broken screenshots are byte-for-byte
equal to their source PNGs:

| Source | Bytes | SHA-256 |
| --- | ---: | --- |
| quota-dark-420.png | 54202 | afc63bba2cbf57d4739390db8aedc91c2d68381f964f85e1049c8e6a2ea468bc |
| dot-mobile-work.png | 28352 | 94f83f95c5676913ed914321e6836f94afe4c086156722e1b4647da376c4de47 |
| dot-mobile-home.png | 42571 | 17404253a1aec8bdf3a4dad122bb47afd8ccb86ae6a0e32baf0a476d1fe600be |

The new browser assertion simulates another Chat activating through the actual
terminal store while an image viewer is open. This covers the viewer lifecycle
when another Chat activates, including the transition used by queued activation;
queue scheduling itself is outside this harness. No forced click bypasses the
modal. A negative control removes
only deactivation cleanup from the served component, without changing Git.

## Validation

- `./scripts/verify.sh all` executed every standard target. Backend tests:
  **3,414 passed / 7 skipped**; frontend tests: **838 passed**; frontend lint,
  types and production build passed. Backend formatting and document checks
  passed. Its only initial failure was a nullable adapter in the new test
  fixture; an explicit non-null assertion fixed the fixture. The complete
  backend type target then passed for **260 source files**, and the affected
  image API test module passed **40 tests**. The first aggregate command's
  nonzero result is retained rather than relabeled as a passing invocation.
- Focused backend tests passed **137** before final validation; final frontend
  focused tests passed **75**. Independent negative and positive probes are
  retained separately under `backend-review/` and `ui-review/`.
- `./scripts/verify.sh browser-mocks`: **9/9 passed**, including the extended
  Chat image scenario. Exact Python/Node/Playwright versions match the locks.
  Evidence: `browser-mocks-evidence/summary.json` and each scenario's report,
  screenshots and completed lifecycle record.
- `./scripts/verify.sh terminal-hmr`: **12/12 passed** with the default timing
  bounds, no skipped/failed/errored cases and a nonempty JUnit report.
- Joint actual Vue + real image API: `browser-positive/report.json` is PASS,
  reports 1920px natural/actual width, original bytes, 390px mobile controls,
  and a closed viewer after another Chat activates. Its context, browser and
  driver all closed normally. The negative control failed exactly at the
  expected viewer-removal assertion; its lifecycle cleanup also completed.
  An earlier negative-harness setup failure is preserved separately and is
  not counted as the behavioral negative control.
- Owned ports **5294, 18279 and 5295** refuse connections after cleanup;
  `owned-server-cleanup.json` records the probe. Browser owners report completed
  API closes and owned-process exit; full descendant process-tree closure is
  deliberately not asserted.
- Full logs and real PNG response digests are retained in the task artifact
  directory. Final staged checks and commit are recorded in the Task handoff.

`terminal-replay`, performance diagnostics and provider/OAuth/Bot connectivity
were not run; these changes do not alter terminal replay or provider execution.

Online production acceptance remains pending integration and deployment. This
review does not establish live provider, OAuth, Bot or external connectivity.
