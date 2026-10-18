# Dot guidance cleanup

## Scope

The user requested short, clear Dot guidance with room for judgment. This changes
repository defaults and generated context, not the live Dot home or its sessions.

- Default role: small requests can be handled directly; longer work can be
  delegated when useful. Prefer task events over routine polling, with targeted
  checks allowed for user requests, stalled work or evidence gaps.
- Turn, wake and tidy guidance: short steps and useful updates are preferences.
  An exact `[quiet]` reply remains the protocol for hiding a quiet Hub turn.
- Tool and review guidance: shorten repeated explanations, keep actual command
  requirements and distinguish delegated judgment from watched tasks and human
  acceptance. Privacy and authorization boundaries remain explicit.
- Dynamic context: use brief suggestions rather than fixed reply lengths or
  unconditional review/notification instructions. Keep complete factual events.

The six active prompt constants total 5,526 characters, down from 7,615 (27.4%).
This compares role, turn, tool, brief, wake and tidy defaults, not tokens or a
single transmitted prompt. Exact historical schedule text is retained only for
migration matching and is not included in new prompts.

## Compatibility and activation

Existing `DOT.md` files are never replaced, even when they contain an older
default. The new default role applies to new homes. Session-level tool/turn
wording is rebuilt on normal or user-authorized rotation; a backend restart
alone continues the existing snapshot. This change adds no forced rotation or
prompt revision mechanism.

Normal schedule setup/start refreshes only exact old defaults for the matching
wake or tidy schedule. Custom wording (including whitespace edits), cross-kind
text and existing user timing are preserved. No live setup, schedule mutation,
message, rotation, service restart, merge or push was performed during this task.

## Verification

Tests cover advisory content with explicit acceptance/privacy/tool requirements,
custom/empty/non-UTF8 role-file preservation, stable session snapshots and
explicit rotation, exact old wake/tidy migration, custom and cross-kind messages,
and existing Dot state, notification and delivery behavior. Backend runtime
resources are test-owned; no model or Feishu service is involved.

- Dot focused scope: 138 passed in 7.59s.
- Strict `verify.sh all`: exit 0; backend 4337 passed / 10 skipped in 220.22s,
  frontend 1029 passed / 0 skipped, mypy 535 source files, and all formatting,
  lint, types, build, documentation and checkout-safety targets passed.
- The clean-delivery provenance test skips on an uncommitted tree and is rerun
  after commit; the other skips are browser/zsh prerequisites.
- Independent delta review passed, including prompt semantics, exact historical
  default matching, preserved customization, session caching and documentation.
- Evidence: `/tmp/ch-dot-guidance-checks/all.log`; no live provider/Feishu or
  browser acceptance was run. These tests do not establish live model behavior.
