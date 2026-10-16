# MASO image input

## Intent and ownership

The MASO Chat composer refused image attachments because the initial HTTP
integration advertised no image capability and only sent text. The installed
MASO service already supports native visual input. Connect the existing Hub
attachment path to that HTTP contract, including image-only turns, queued
drafts and durable display history.

- Task: `bbd70b88-f199-495e-ace1-d9a60e9a9df5`, initiator control, epoch 1;
  retain configured reviewed mode, no Workspace dispatch.
- Workspace: `79f6acfd-dbde-4951-ab3d-8abceeec1736`.
- Branch: `codex/maso-image-input`, base `2a43b616c65ce6dd15c78633ff48d7a4cea14883`.
- Checkout: `/home/tiger/claude_hub_worktree/maso-image-input`.
- Root is the sole writer. Native researchers checked installed MASO and Hub
  contracts; a separate native reviewer reviewed the complete candidate and
  reran the focused backend tests. No delegated Task reporting or key access.
- This delivery does not merge, push, or restart the primary Hub or retained
  user preview. Preserve unrelated primary Feishu changes and `start.sh`.

## Installed protocol and implementation

Checked MASO v0.7.3 build `2980215402e7d534a599355294c6b2521a7fa538`
against its OpenAPI and installed `server/images.py`, official Chat client,
context schema and Azure Responses adapter.

Upload each original image through the active session's
`POST /api/sessions/{sid}/images/chunk`, using a UUID hex upload ID, zero-based
sequence and final flag. Chunks contain raw binary bytes and use 512 KiB, with
30-second request timeouts and a 120-second overall image-upload deadline.
Intermediate responses are 204; final metadata must identify this exact upload
ID and match the MIME type and byte count. PNG, JPEG, GIF and WebP are supported
up to 8 MiB per image; BMP is rejected. Validate every attachment before any
session creation or image upload.

After all uploads succeed, send the optional text and session-owned `image_ref`
parts in one `/messages` POST. Keep text-only requests in their existing string
form. Do not send Hub paths, protected preview URLs, or large inline data URLs;
this contract also works when MASO_BASE_URL points to a remote service. Continue
using the selected model key without changing MASO's global model setting.

Advertise `supports_images` on both the provider and returned capabilities.
The existing composer, queue, preview cache and history need no product UI
change. Originals go to MASO; display previews and opaque attachment IDs stay
in Hub. Ignore MASO's user-message echoes so its image data URLs do not become
duplicate durable display events. Opening history does not resend images.

Upload failure occurs before the message POST, releases the turn guard and
restores first-turn guidance for retry. Transport stop/coroutine cancellation
cannot let a delayed upload start a later turn. This does not change the
existing composer behavior: Stop is disabled during an outstanding send,
including upload, and the tailer serializes send/cancel. Once a message POST
starts, existing uncertain-delivery quarantine and recovery remain in force.
Unused remote refs expire under MASO's policy; Hub does not delete shared
service files. Fork seeding remains text-only.

## Independent review and repairs

The reviewer identified that MASO returns `ref == upload_id`, while the first
mock generated another random reference. Tightened the response check and added
a valid-hex wrong-reference regression. Corrected two new test mistakes: the
tailer cursor must include sequence zero, and the adapter entry point is
`normalize_line`. The first focused runs failed those tests/type checks; after
repair, both owner and reviewer independently passed 102 tests. Reviewer
evidence: `/tmp/claude-hub-verify.HMpKC3`; owner evidence:
`/tmp/claude-hub-verify.4o3bJZ`.

The new browser image-only/queue scenario exposed inaccurate old mock behavior:
`/wait` advanced its cursor while returning no events, and authoritative turn
IDs differed from client turn IDs. Updated the fixture to deliver new events,
retain an empty response's cursor, and use the submitted client ID throughout
send/cancel. Retained all 108 preexisting assertions; the final fixture has 125.
The separate reviewer approved the corrected fixture and final evidence.

## Validation and live acceptance

Use the repository's strict tool versions: Python 3.11.16, Node 24.21.0,
uv 0.12.15, pnpm 9.15.9 and Playwright 1.58.0.

- `./scripts/verify.sh all`: PASS. All nine targets passed; backend 3297 passed,
  7 skipped; frontend unit tests 781 passed. Backend types checked 249 files.
  Backend full-suite evidence: `/tmp/claude-hub-verify.m2gXUb`.
- `./scripts/verify.sh browser-mocks`: PASS, 9/9, including multiple image-only
  attachments, queued text/image originals, history reload without resending,
  desktop/mobile composer and existing MASO preflight recovery. No unknown API
  requests or unexpected external requests. Evidence:
  `/tmp/claude-hub-browser-mocks.BPylSo/run`. All owned browsers/Vite closed;
  port 5313 no longer accepted connections.
- Real browser → isolated Hub 18307/Vite 5314 → MASO 9796:
  `gpt56_sol_reasoning_xhigh` correctly answered
  `左侧是红色，右侧是蓝色。` for a 320×160 PNG. The prompt asked for the left/right
  colors without revealing them. One send, no tools or API mocks. Original
  image bytes were retrieved from Hub's cache; normalized events contained
  attachment IDs and no image data URL/ref. Reload decoded the image and did
  not send another turn. Private DOM checks covered desktop/mobile; the real
  mobile screenshot has an open sessions drawer, so use the mocked mobile
  screenshot for visible layout evidence, not that obscured capture.
  Evidence: `.agent_office/maso-image-input/live-evidence/summary.json` and
  `live-image.log`. Test-owned Hub tab deleted, MASO session archived, private
  backends/browser/tmux stopped, ports 18307/5314 closed. No model-global writes
  or service restart.
- Extra terminal HMR gate: the first run passed two cases and failed the
  existing working-Claude remount `content-pending` assertion, matching the
  previously recorded failure in `2026-10-13-maso-main-integration.md`. Evidence:
  `/tmp/claude-hub-verify.PRMR6k`. A separate serial run also passed two cases
  and failed that assertion, leaving nine not run; evidence is
  `/tmp/claude-hub-verify.bDIHJi`. No HMR/terminal product code or assertion was
  changed. Independent validation in the clean base checkout passed that case
  alone (1 passed, `/tmp/claude-hub-verify.zw3Ies`), which does not establish the
  cause of the full-sequence failure. The reviewer then ran the matching full
  sequence on that clean base and passed all 12 cases in 93.93 seconds;
  evidence is `/tmp/claude-hub-verify.HRrfnz`. Its 12 owned Vite ports and private
  tmux servers were closed, and the checkout remained clean. The candidate's
  two failures remain unexplained: the passing base is not proof of an
  established baseline flake. This optional gate did not pass for the candidate;
  keep that limitation separate from image acceptance and the passing standard
  suite. No additional retries, assertion changes or unrelated terminal fixes.

The installed catalog has no per-model visual capability field. Real inference
was verified for the user's selected gpt56_sol Extra High variant with PNG;
other model/upstream combinations were not independently invoked. Remote
endpoint behavior, large chunking and narrower format/size rejection are covered
by mock contracts; the live model check used the local service and a small PNG.

Primary baseline and protected-file hashes are recorded in
`.agent_office/maso-image-input/initial-boundaries.json`. The real acceptance
confirmed the primary Hub and retained user preview instance IDs did not change.

## Final handoff checks

Final formatting and documentation checks passed after the browser fixture and
working-log changes. `git diff --check` passed; AGENTS.md and CLAUDE.md remain
byte-identical. All protected primary-file hashes match the initial snapshot,
including the unrelated Feishu changes and start.sh; primary HEAD remains the
base commit. The image candidate received independent approval, including a
separate final fixture/evidence review. Explicit staging includes only the six
intended files; `./scripts/verify.sh check-staged` passed. Commit/branch delivery
and the unresolved extra terminal-gate limitation are reported through the same
Task. No main integration or runtime update is part of this handoff.
