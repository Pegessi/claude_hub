# Stage three follow-up: main integration and attachment acceptance

Date: 2026-10-13. Worktree: `quality-stage3-boundaries`, branch `feat/quality-stage3`.
No production restart, main-branch update, push or dependency download.

## Main integration

The fixed integration commit is
`e2dc06de3ec7906c24f8ab7b53cb8fa1c7e801c0`. It has the previous Stage 3
candidate and the fetched main tip as parents. Their identities, reconstructed
merge and eight decisions are recorded in
[the fixed-merge record](2026-10-13-stage3-main-merge-decisions.json).
The record certifies decision coverage, not semantic correctness or later commits.

The two native text conflicts were the Unreleased changelog insertion and
`verify.sh`. Both changelog sections were preserved. Upstream had just changed
local version drift from a failure to a warning; a textual combination alone
would still have failed in Stage 3's Python provenance checker.

Version policy now belongs to the existing shell `check_tool_version` helper.
The provenance checker retains its interpreter, editable source and tool-location
checks and reports the actual Python version. The same isolated Python entry
reads that successful report before the shared policy decides whether to warn or
fail. Source failures, unreadable reports and missing/non-string/empty versions
always fail. CI, explicit strict mode and browser-mocks retain strict versions.
No second version policy or provenance bypass was added.

Parent-preservation checks found 34 upstream-only paths identical to upstream and
43 Stage-3-only paths identical to the previous candidate. Five shared paths and
four Stage-3-only coordination paths were reviewed separately. The resulting
merge tree was frozen before testing and matched the committed tree.

## Attachment acceptance and CSS cleanup

The existing Task creation browser scenario now:

- Dispatches a real bubbling, cancelable ClipboardEvent with a PNG File through
  the dialog's Vue listener; checks that the image is decoded and displayed.
- Confirms a title with empty description and a non-empty attachment can submit.
- Closes an unstaged draft and confirms reopening clears its attachment preview.
- Simulates a connection reset after the mock server has created the Task.
- Closes/reopens the uncertain request and checks that explicit retry preserves
  the complete request body, raw body, request key and attachment bytes.
- Checks one stored Task by stable ID and one rendered card by its exact heading.

The mock receipt uses the actual stored-attachment fields instead of returning
an empty attachment list. APIs remain fully mocked; this is not live Hub or
Provider acceptance.

Creation-only selectors no longer used by the parent were removed. The dialog
also drops copied selectors for unrelated controls. Shared parent styles,
matching child styles and responsive rules remain. Both Vue files' template and
script prefixes are unchanged. The acceptance table was updated without adding
new workflow rules or expanding the required Agent entry guide.

## Executed checks and retained failures

Evidence root: `/tmp/claude-hub-stage3-followup.PDhKKD`.

- Merge coordination: 98 focused backend tests passed.
- Frozen merge: `all` passed with strict versions, 3242 backend passes and seven
  skips, 775 frontend tests, type/format/lint/build checks passed. All nine browser
  scenarios also passed. The dirty-tree provenance skip is checked after commit.
- First attachment run: eight scenarios passed; the Task scenario failed because
  `has_text` matched another card's Related task option. The new locator now uses
  the card's exact heading; count and stored-ID uniqueness assertions remain.
  This was a test locator error, not evidence of duplicate Task creation.
- Corrected attachment run: all nine browser scenarios passed.
- Mutation: temporarily returning an empty list from `serializeDraftAttachments`
  made only the new exact attachment-payload assertion fail. Browser lifecycle
  cleanup completed and Vite exited normally. Original product bytes were
  restored in `finally` and compared with both the saved file and committed blob.
  The negative run is retained separately; no mutation remains in the candidate.

Final combined validation is recorded under `final-checks/` in that evidence
root. Browser and HMR/replay targets are explicit and are not implied by `all`.
Final counts and any remaining limitations belong to the delivery record.

## Review and limitations

The merge coordination received independent static review separately from the
author's self-review. The attachment scenario and actual selector removals were
independently reviewed; the exact-heading correction was checked against the
card template after the first real run exposed the earlier selector mistake.
Master executed the tests, inspected the desktop recovery and mobile dialog
screenshots, and checked raw reports. Static review is not test execution.

No conclusion is made about arbitrary attachment formats, every clipboard
platform, or every future use of the shared helper. No real Provider, Bot or OAuth
round trip was run. Existing browser lifecycle reports retain
`process_tree_exit=not_verified`. The integration targets the recorded upstream
commit; a later upstream change needs its own integration decision.
