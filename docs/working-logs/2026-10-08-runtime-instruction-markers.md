# Descriptive Hub instruction markers

## Scope

This follows `60689f9` on `feat/agent-workflow-v2`. New native Chat input uses:

```text
<claude_hub_instructions>
You are running inside Claude Hub. The `claude-hub` CLI is available on PATH.
...
</claude_hub_instructions>
```

The actual introduction continues with the existing environment-based Chat ID
instruction. Other guidance, first-turn injection timing, Task/reporting
permissions, and the separate question-protocol and fork-seed markers are
unchanged. The tags identify a text block; they do not confer instruction
priority or additional permissions.

## Historical compatibility

Provider transcripts may contain `<<<HUB_RUNTIME_V1>>>` and
`<<<END_HUB_RUNTIME_V1>>>`. New sends do not emit them, but the shared
`strip_hub_runtime_guidance` helper still recognizes that exact pair.

The helper selects the earliest supported opening marker and searches only for
its matching closing marker. Without that close, it returns the original text,
even if a later block of the other format is complete. It removes one block per
call, preserves text before it, and retains the existing newline-only stripping
after the block. This is literal marker handling, not a general XML parser.

Claude/Codex/Cursor history adapters and the supported edit/fork text extractor
already call this helper, so they need no independent marker implementations.
No transcript, runtime-state file, or credential is migrated or rewritten.

## Verification

Tests cover new-only outbound markers and natural-language introduction,
first-turn-only injection, independent fixed legacy fixtures, missing and
mismatched closing markers, earliest-block selection, prefix/user whitespace,
provider normalization, composition with image/question prefixes, supported
edit-text extraction, and coexistence with fork-seed history.

The native/policy, fork-seed, Cursor transcript, edit-resend discovery/recovery,
and prompt-measurement suites passed: **218 tests**. All **122 backend source
files passed mypy**; the three changed Python files passed Black/isort checks.
Evidence: `/tmp/claude-hub-runtime-markers.YqlhRb`.

Independent read-only review of the exact native and two test objects found no
new issue in scope. The reviewer did not execute tests. The checked transports
are mocked and use private runtime homes; this is not a real-model acceptance
run. No real provider, Bot/OAuth, main merge, push, deployment, or production
restart was performed.
