# Image-display fix: main integration and streaming regression

## Scope and merge

Integrate image candidate `6b012eb` with main `91cf651`, without the separate
concept/workflow integration candidate. Merge `82ed969` preserves both parents'
source changes. The only conflict was the two new CHANGELOG entries; both were
retained. The fixed-merge record is
[2026-11-09-image-merge-decisions.json](2026-11-09-image-merge-decisions.json).
No push or shared-service restart is part of this integration.

## Defect found during independent cross-verification

A fresh Claude Code reviewer mounted the real `MarkdownContent`, let a local
image fail, then appended five text deltas to the same paragraph in that same
instance. Requests grew from 1 to 6. The existing full-page image fixture had
only one initial text delta and could not detect this streaming regression.

`neutralizeKnownFailures` was behind the Mermaid-only early return. The watcher
already used `flush: 'post'`; its flush timing was not changed. Fix `36c2aa6`
runs failed-image handling after every rendered update with a root, before
checking whether Mermaid rendering is necessary. Mermaid cancellation, timers
and theme observation remain unchanged.

The reviewer reran the unchanged probe on the fixed source: one mount, five
deltas, request counts `[1, 1, 1, 1, 1]`. Manual Retry made one request, decoded
the recovered image, and restored both mouse and Enter preview events. A later
text update did not restore the stale failure placeholder. A separate real
`ChatAgentImage` probe kept `tool.status=running`, toggled `turnActive` on the
same instance, and verified both successful-image and failed-image/Retry
settlement. Neither probe contacts a real provider.

## Permanent regression and negative control

The existing `browser_chat_image_quality.py` scenario now also drives
`image_streaming_harness.html`. The fixture mounts the production component
once with a reactive paragraph and exposes only text input and observation
counters; it does not implement image/error/retry behavior. The test waits for
the first failed placeholder, then makes five separate same-paragraph updates,
yields rendering frames, and checks request count, one block and one mount.
It also checks manual recovery, mouse/keyboard preview events, and a further
post-recovery update. The existing SPA checks still exercise the actual preview
dialog. This is component rendering plus mocked HTTP, not live integration.

The exact new assertion was run against both versions through owned Vite
servers. A test-only Vite loader substituted the original `121a794` component
source without editing the checkout: the first delta failed with 2 requests.
Without that substitution the new test passed all five deltas with 1 request.
Both runs retained lifecycle evidence and closed their owned ports/runtimes.

## Baseline validation fixes

The initial integrated standard run passed 6172 backend tests (9 skipped) and
1262 frontend tests. Format/type checks found two test-only errors already
present byte-for-byte in main `91cf651`: Black formatting of the Feishu
provenance assertion, and Optional BotEntry access in the group-binding test.
Commit `3b2dc88` formats that assertion and checks the entry before reading its
binding. Backend format/types and all 9 group-binding-store tests then passed.
No Feishu product behavior or test expectation was removed.

The isolated terminal-HMR gate passed all 12 cases. Its first setup attempt
could not resolve pnpm through Corepack with private HOME/network disabled;
the rerun used the already-cached pinned pnpm 9.15.9 executable. No dependency
lock, shared environment or service was changed.

Final gate results and source identities are retained with controller Task
`d9518419-c395-47d9-a887-2ba85232c811` and independent reviewer Task
`db12288d-3aca-4304-b4f2-149276be8f31`. Old failed runs remain evidence, not passes.
The older concept/workflow candidate and its browser limitation are separate.
