# Claude model picker from tab environment

## Symptom

After Claude Code was upgraded, a Claude Chat tab still offered old model ids
such as `claude-opus-4-8`. The tab itself was configured to use a newer or
gateway-specific model through `ANTHROPIC_MODEL`, so selecting a picker entry
could send an unsupported id to that gateway.

## Root cause

Claude Code has no model-catalog command comparable to Cursor's
`agent --list-models`. Claude Hub therefore returned one process-wide curated
list for every Claude tab, even though each tab can target a different gateway
and model catalog through its launch environment. Upgrading the CLI could not
change that hardcoded list.

## Behavior

Claude native Chat capabilities now build model options, in priority order,
from the selected tab's:

1. `ANTHROPIC_MODEL`
2. `ANTHROPIC_DEFAULT_OPUS_MODEL`
3. `ANTHROPIC_DEFAULT_SONNET_MODEL`
4. `ANTHROPIC_DEFAULT_HAIKU_MODEL`

Empty values are ignored and repeated ids appear once. If none of these
variables supplies a model id, Hub retains the existing curated static list as
a fallback. The environment-derived list is session-local and is rebuilt on
capabilities preparation, so it is not shared through the global provider
catalog cache.

After the model picker successfully updates a tab environment, the frontend
refreshes that active stream's capabilities in place. The refresh has its own
abort owner and checks the active session and stream path before committing, so
a late response cannot overwrite a subsequently selected tab. Timeline event
delivery remains active and history is not rehydrated.

## Validation

- A regression test first failed against the old static options, then passed
  with one current model and distinct Sonnet/Haiku defaults while deduplicating
  the repeated Opus id.
- Focused backend regression: `3 passed, 157 deselected`.
- Focused frontend model/capability regression: `7 passed`.
- The task-owned Vite review server returned HTTP 200 on `127.0.0.1:5279` and
  was stopped after review. The shared Hub was not restarted or modified.
- `./scripts/verify.sh all` with the repository-pinned uv, Node, and pnpm passed
  docs, backend format, backend types, frontend lint, frontend types, all 702
  frontend tests, and the production frontend build.
- The full backend target reached `1803 passed, 1 skipped` before the unrelated
  `test_frontend_build_timeout_keeps_current_backend_and_dist` failed because
  macOS returned `EPERM` while that test tried to inspect its build subprocess.
  A second full-backend run stopped at the same test with the same counts and
  error; the failing test passed when rerun alone (`1 passed`).
- `git diff --check` passed, and `AGENTS.md` remains byte-identical to
  `CLAUDE.md`. A final diff review found no additional actionable issue after
  adding the in-place capability refresh.
