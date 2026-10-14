# MASO image input cross-review and main integration

Task `38827d1c-caa1-41fd-98f3-70403983d167`, initiator control with configured
review policy retained. The user explicitly authorized cross-review of the
image feature and main integration/push if sound. No shared-service restart is
included.

## Candidate and fixed merge

Worktree `~/claude_hub_worktree/maso-image-integration`, integration branch
`codex/maso-image-integration`:

- Main parent: `8079d3c19fdc057793a658615366b753967c92e4`.
- Image parent: `96c11f03d3d4b296066dc6aae94f6c7a5b6ed7f3`.
- Fixed merge: `9b77b049b07f2c3dd94f79770d5ac3979f1856ac`.
- Tree: `0165bba0eec33a62a87eddf9cd7c0def81155d4b`.

Only `CHANGELOG.md` had a native conflict. The resolution retains the image
entry and both startup configuration/discovery entries. Source, tests and MASO
documentation merged automatically; no source behavior was manually transplanted.
The exact merge's [decision record](2026-10-15-maso-image-merge-decisions.json)
passes owner and independent coverage checks with one resolved path.

## Independent cross-review

Two read-only native reviewers checked the actual fixed result:

- Protocol/reliability: no actionable findings. Inspected installed MASO v0.7.3
  `images.py` and message resolver as well as native send/cancel/tailer paths.
  Chunk ordering/final metadata/session refs, original bytes, image-only/multiple
  images, safe upload retry, generation fencing and uncertain POST quarantine
  match the implementation. Independently reran 33 HTTP contract tests, all
  passed; evidence `/tmp/claude-hub-verify.zgX2tU`.
- UI/history/integration: no actionable findings. Checked capability admission,
  composer/queue/cache/display paths and meaningful byte/count/cursor/history
  browser assertions. Independently checked the fixed merge record, whitespace
  and byte-identical AGENTS/CLAUDE instructions. Current startup/.env URL
  precedence and per-Chat endpoint binding remain intact.

## Fresh validation

- `./scripts/verify.sh all`: exit 0; all nine standard targets passed. Backend
  3320 passed, 7 skipped, 33 warnings. Frontend lint/types/build and 781 unit tests
  passed. Log: `.agent_office/maso-image-integration/verify-all.log`.
- `./scripts/verify.sh browser-mocks`: PASS, 9/9; evidence
  `/tmp/claude-hub-browser-mocks.AGt7l6/run`. Includes image-only submission,
  queued originals, history decoding without resending and current MASO
  unavailable/retry states against mocked APIs. The mobile image-history
  screenshot was inspected. Its owned Vite exited with code 0 and port 5327
  was closed by the runner.
- Complete terminal HMR gate: PASS, 12 passed, 2 warnings in 94.80 seconds;
  pytest and JUnit validation exited 0. Evidence
  `/tmp/claude-hub-verify.Xa4HwI`, with private TMPDIR `/tmp/ch-hmr-LjOnQ0`.
  Includes the historical failing `content-pending` remount case. This fresh
  required run was serial after full/browser checks; no assertion or HMR product
  code was changed to obtain the pass. Historical failures remain historical,
  not an established baseline flake. No owned browser/Vite/backend process
  remained after the fixtures/runner exited.

Private logs, locked-tool wrapper and primary snapshots are under
`.agent_office/maso-image-integration`. Dependencies were installed independently
in this checkout with the declared tools; no main environment was copied.

## Preservation and limits

Primary main/origin matched the main parent. All nine existing dirty/untracked
files, their modes/hashes and the running backend instance identity were privately
recorded. These local modifications are outside the image delta. Before delivery,
guard the main/remote tips, fast-forward only the reviewed candidate, verify all
local bytes/modes and the service instance, then push main. Do not reset, clean,
modify real configuration or restart shared services.

This integration does not repeat model inference. The original feature's live
acceptance covered a small PNG and one selected model; fresh browser evidence
uses mocked APIs, while other formats/large chunks/remote transport rely on
contract tests. Per-model visual support is owned by MASO/upstream. Existing
Chats keep their endpoint; a Chat pinned to an incorrect old address needs a new
Chat for the intended service.
