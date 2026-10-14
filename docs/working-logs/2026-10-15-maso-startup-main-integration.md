# MASO startup configuration main integration

Task `ab87c287-b2a9-48d5-b706-5a2ecee99265`, initiator-controlled with configured
review policy retained. The user authorized main integration; service restart
remains the user's action. This integration includes local URL discovery,
startup diagnostics, `Settings` / `backend/.env` configuration and skill guidance.
Image support is a separate `codex/maso-image-input` feature and is not included
in this candidate.

## Fixed merge

- Checkout: `~/claude_hub_worktree/maso-startup-integration`.
- Integration branch: `codex/maso-startup-integration`.
- Main parent: `2a43b616c65ce6dd15c78633ff48d7a4cea14883`.
- Feature parent: `8f8412a6b845566b9639d8a0777e36f1da7e6e98`.
- Immutable merge: `1c67955d0bf14bbfbfaba9c3382209c105d9517a`.
- Result tree: `e7e18dee9a0d584865772556800ce19d8a9101af`, identical to the
  reviewed feature tree. No native conflicts or manual decision paths.

`verify.sh check-staged` passed before creating the conventional merge commit.
Owner and independent reviewer both passed `verify.sh merge-decisions` for the
exact merge with zero decision paths; no decision record is required. Owner's
inspection JSON is retained at
`.agent_office/maso-startup-integration/startup-merge-decisions.json`.

Independent read-only fixed-merge review passed with no actionable findings.
The reviewer checked exact parents/tree, configuration/readiness/session paths,
the exclusion of image commit `96c11f03d3d4b296066dc6aae94f6c7a5b6ed7f3`, and the
primary-checkout preservation procedure. `AGENTS.md` and `CLAUDE.md` remain
byte-identical.

## Validation

Locked Python 3.11.16 and pnpm 9.15.9 dependencies were installed independently
in this integration checkout. Full `verify.sh all` output is retained in
`.agent_office/maso-startup-integration/verify-all.log`.

Final `./scripts/verify.sh all`: exit 0; all nine standard targets passed.
Backend behavior: 3309 passed, 6 skipped (explicit browser opt-in), 33 warnings.
Backend formatting/types and frontend lint/types/unit/build all passed. Browser,
terminal HMR/replay and real inference were not run by this integration.

A fresh read-only probe without either MASO URL/port environment variable found
the actual local service at `http://127.0.0.1:11004`, advertising 8 models. This
does not establish upstream credentials/quota or inference success. No image or
model inference is part of this integration's validation.

## Primary preservation and delivery procedure

Before integration, primary main matched origin and contained eight modified
files plus one untracked Feishu test. All nine files were privately copied with
status/mode/hash metadata into the task's `primary-before` evidence directory.
Only `start.sh` overlaps the candidate: its local default listener changes
`0.0.0.0` to `::`. Applying that local patch to the reviewed startup file was
verified in task-owned evidence, producing an expected final hash.

After final validation/review, fast-forward primary main to this integration
branch. Protect only the overlapping `start.sh` with a path-limited stash and
restore that exact stash SHA. Verify all other dirty/untracked file bytes/modes,
the expected combined startup file, unchanged staged state, and the primary
backend instance identity before pushing main. Retain the private backups; do
not reset, clean, delete unrelated files or restart the primary service.

The user can restart the service after delivery. New MASO Chats use the configured
or uniquely discovered service. An existing Chat pinned to a wrong old URL keeps
that identity and requires a new Chat for selecting the intended service.
