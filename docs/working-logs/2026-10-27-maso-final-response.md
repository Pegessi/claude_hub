# MASO final response event repair

## Incident and evidence

The first turn of Hub tab `cb3de306-de92-47d2-90e2-bece23253780` completed without
visible assistant text. The authenticated Hub event API was read through
`has_more=false`: 643 events at capture time. Epoch 1 ended at sequence 622 with
`status=completed`, but its root agent had zero `text_delta` events. Child text
was present and correctly attributed.

Read-only inspection of the associated MASO 0.7.5 service established:

- Session `57d38204-a18` retained the complete first final answer in history.
- Bounded SSE replay retained `post_llm_response` block `420c1a1b72574a42`, with
  1,136 characters of final content, followed by metadata-only `turn_end` and root
  `agent_end`.
- The installed `maso_cowork/server/plugin.py` emits authoritative content and
  reasoning in `post_llm_response`. Its `turn_end` payload only identifies the
  model call, outcome and message; it does not contain the reply.
- Hub's adapter ignored `post_llm_response` and only repaired streamed text from
  a content-bearing `turn_end`. Without volatile `llm_token` content, the complete
  response was lost before persistence. This is a Hub provider-adapter defect.

Raw local captures remain outside Git in a private evidence directory. No model
calls, replayed user commands, history mutations or service restarts were made.

## Change boundary

Reuse the adapter's existing per-agent, per-epoch suffix reconciliation for
`post_llm_response`. Keep content-bearing `turn_end` compatibility and ignore
`dropped=true` snapshots. No history fetch, transport reconnect, inferred answer,
new completion event, frontend change or live-store backfill is introduced.

Root `agent_end` still owns completion. Child responses stay in their child
threads, and initial replay remains excluded by the transport baseline.

The older implementation log described `turn_end` as a response snapshot; the
current supported contract is corrected in `docs/MASO.md`.

## Validation

- Before the adapter change: the new/current-protocol cases failed as expected
  (4 failed, 1 legacy case passed). Failures included absent `assistant_text`,
  truncated final text and missing reasoning.
- After the adapter change: 60 tests passed across `test_maso_http.py` and
  `test_maso_error_detail.py` through the isolated repository verifier.
- Coverage includes absent/partial/complete token streams, duplicate snapshots,
  tool work before the final reply, child attribution, initial replay exclusion,
  dropped frames, condensation, legacy snapshots and Goal completion fencing.
- Replayed the captured provider frames through both adapters without contacting
  a model: the old adapter produced zero root-text characters; the patched
  adapter produced all 1,136 characters, exactly matching the saved answer, with
  one root completion in both runs.
- Repository verification: docs, checkout safety, backend format and types,
  frontend lint/types/build and all 1,136 frontend unit tests passed. The frontend
  verifier warned that its outer pnpm command resolved to 12.5.1 rather than the
  declared 9.15.9; these frontend results are noncanonical. Dependency installation
  used the frozen lockfile and pnpm 9.15.9.
- The first full backend run stopped at the unrelated CI diagnostic parser:
  inherited forced-color output prefixed `ERROR:` with ANSI escapes. Rerunning
  only the backend test target with `FORCE_COLOR`/`PY_COLORS` unset and `NO_COLOR=1`
  passed: **5,438 passed, 10 skipped**, 85% overall coverage. No source change or
  test exclusion was needed for that environment correction.
- Independent read-only review approved the candidate without blocking findings.
  It confirmed the current upstream event schema and root/child boundaries.
- Browser/manual UI checks were not run; no frontend files changed. The live
  shared service remains unchanged, and previously persisted turns are not
  automatically repaired.

## Existing limitations outside this fix

The append-only reconciler does not replace a rewritten prefix or retract
already-persisted token deltas after a dropped response. The dropped-frame test
only verifies that the snapshot itself adds no text. These behaviors predate this
change; this repair handles absent tokens and suffix-compatible snapshots.
