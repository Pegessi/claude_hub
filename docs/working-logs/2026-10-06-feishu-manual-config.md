# Manual instance Feishu Webhook Bot configuration

## Scope and deployment status

This candidate adds a single instance-wide Bot settings dialog to Extensions.
Per-Chat pairing remains in the existing Feishu binding panel. Bot configuration,
Chat bindings, and historical message origins are separate state.

The work is based on `6642d2cd9ec950c504ab626ea03a92beff4353bf` in
`feat/feishu-manual-config`. It does not deploy or restart a service, change an
existing Feishu application, configure Web OAuth, or add multi-Bot, long-connection,
shared input queue, or approval-card support.

## Operator setup

1. Configure legitimate Feishu OAuth for the instance using the existing
   deployment procedure. Bot and OAuth App IDs must match; application-scoped
   open IDs are not mapped between applications.
2. Set `CLAUDE_HUB_FEISHU_BOT_ADMIN_OPEN_IDS` to an explicit comma-separated list
   of permitted OAuth open IDs. Normal sign-in permission does not grant Bot
   administration. Local-network identity and auth-disabled mode do not bypass
   this check.
3. Configure `CLAUDE_HUB_PUBLIC_BASE_URL` or the existing
   `CLAUDE_HUB_PROVIDER_PUBLIC_URL` if the UI should display a Webhook URL. The URL
   is derived only from explicit deployment configuration, never request Host
   or forwarding headers.
4. Sign in as an allowed administrator and open **Extensions → Feishu Bot
   settings**. Enter App ID, App Secret, Verification Token, and Encrypt Key.
   Every save requires all four values; secret values are never read back.
5. **Validate and save** validates App ID/App Secret through the tenant-token
   endpoint before publication. It does not validate callback delivery, event
   subscriptions, the matching callback token/key in Feishu, or Agent execution.
6. Configure the authorized application's encrypted Webhook and required event
   permissions in Feishu. Separately verify a real callback and message round
   trip. Pair each Chat from its existing binding panel.

Complete environment configuration retains read-only precedence. Partial
configuration is invalid; it is not combined with saved credentials. Existing
environment-mode encryption compatibility is unchanged, while manual saves
require a nonempty Encrypt Key. Environment-managed Bots must be changed or
removed through deployment configuration, not this dialog.

## API and UI boundaries

| Endpoint | Access | Result / mutation |
| --- | --- | --- |
| `GET /api/feishu/bot/config/status` | Real authorized OAuth user | `configured`, `source`, `can_manage`, `editable`, `event_url`, `revision` |
| `GET /api/feishu/bot/config` | Explicit Bot administrator | Safe status plus App ID, secret-presence flags, and update time; no secrets |
| `PUT /api/feishu/bot/config` | Explicit Bot administrator | Four credential strings, `expected_revision`, `allow_app_id_change` |
| `DELETE /api/feishu/bot/config` | Explicit Bot administrator | JSON body containing `expected_revision` |

An absent saved configuration starts at revision zero. Environment mode has a
null revision and is not editable. Editable saved/absent states have an integer
revision. Malformed requests return fixed errors rather than validation responses
that might echo submitted secret values.

The UI stores secret inputs only in component refs. Closing, unmounting, successful
save, or configuration reload clears them. Validation failure retains inputs for
correction. Revision conflicts reload current state and clear inputs without
retrying the mutation. App ID changes require a separate explicit confirmation.
Clipboard errors do not trigger configuration reload. Closing an in-flight request
cannot undo an already submitted server operation; reopening reads current state.
A 500 response, or a `public_url_invalid` response after a mutation, may follow a
successful commit. The UI reloads current state without repeating PUT/DELETE and
clears stale editable state if that read also fails. A transient
`config_operation_busy` response preserves inputs and advises waiting.

## Storage and concurrency

- Saved configuration lives at `<runtime home>/secrets/feishu_bot.json` with a
  private directory and mode-0600 files. Publication creates the temporary file
  with restrictive permissions, flushes it, atomically replaces the destination,
  and syncs the directory.
- `revision` advances on every save/deactivation, invalidating stale callback
  snapshots and stale compare-and-swap requests.
- `binding_generation` advances on initial enable, deactivation, and confirmed
  App ID changes. Same-app credential rotation preserves Chat bindings.
- Configuration publication precedes physical binding/code cleanup. If cleanup
  fails after publication, the new revision/generation remains authoritative;
  old routing is rejected even if old records are still on disk. History and
  inbound deduplication records are not removed.
- Tokens and model turns run outside the publication gate. Every outbound reply
  reacquires the gate, checks the current configuration and exact binding, and
  performs one POST with a 20-second overall timeout. Gate acquisition is also
  bounded. A successfully completed deactivation prevents a new old-snapshot
  reply from starting; an already sent external request cannot be recalled.
- Delivery with an uncertain result is not retried. Post-reply binding cleanup
  failure must not enter a second user-facing failure-reply path.
- Missing fields, invalid UTF-8, invalid revisions/generations, and timestamps
  that cannot be represented by the API fail closed. The store does not cache
  an older valid configuration when current persisted state is invalid.

## Verification procedure and limits

Backend checks run from the canonical feature worktree with private HOME, XDG,
runtime state, temporary directories, and tmux socket names. Both Bot test modules
block default HTTP/HTTPS transports, including regression failure paths; explicit
ASGI and MockTransport clients remain available. No real credentials are needed.

```text
python -m pytest tests/test_feishu_bot_config.py tests/test_feishu_bot.py tests/test_public_base_url.py -q
python -m mypy claude_hub
node --test tests/*.test.mjs
eslint . --ext .vue,.js,.jsx,.cjs,.mjs,.ts,.tsx,.cts,.mts
vue-tsc
vite build
```

`frontend/tests/browser_feishu_bot_config.py` drives the actual built SPA against
an owned numeric-loopback static server. All APIs are mocked, unknown API paths
fail the final assertion, WebSockets are closed, service workers are blocked,
and non-task-origin requests are aborted. Known external font and preconnect
links are removed before rendering. The browser executable must already exist.

```text
python frontend/tests/browser_feishu_bot_config.py \
  http://127.0.0.1:<owned-port> <owned-output-directory> <existing-chromium>
```

The controller must close its static server and verify the listener is gone even
when browser assertions fail. The browser test does not start Hub, tmux, or a
provider. It is UI behavior evidence, not authentication or Feishu E2E evidence.

File replacement and file/directory fsync failure points have not each received
direct fault injection. The post-publication binding-cleanup regression should
not be described as covering every storage failure. Real same-app OAuth,
Webhook configuration, Bot delivery, and provider acceptance remain separate
verification steps. No production restart, main merge, or push is part of this
candidate's verification.

## Recorded candidate results

Evidence root: `/tmp/claude-hub-takeover-tests.KvXCMa/` on the validation host.

- `manual-bot-cleanup-before`: both new post-reply cleanup-failure cases failed
  on the old behavior, demonstrating the duplicate POST before the fix.
- `manual-bot-offline-final`: **67 passed**, 67 warnings; full backend mypy passed
  for **123 source files**. Both commands recorded exit zero. Canonical
  100-column Black/isort formatting subsequently passed, with AST equality
  checked for all five affected backend files.
- `manual-bot-browser/*-release.log`: **646 frontend tests passed**, ESLint with
  no warnings, TypeScript check, and Vite build passed; each recorded exit zero.
- `manual-bot-browser/run-1`: failed on the test's over-exact accessible label
  selector, not a credential request. The selector was corrected; the static
  server was closed even on failure.
- `manual-bot-browser/run-4`: **PASS**, exit zero, 52 fully mocked API requests,
  no unrecognized API paths, no page errors, and no external requests. Coverage
  includes explicit App ID confirmation, input preservation on busy responses,
  and state reconciliation after a post-commit DELETE failure without another
  DELETE. The console contains only the five intentionally mocked HTTP errors.
  Static server port **42841** was closed and absence of its listener verified.
- Dedicated independent reviewers checked backend authorization/publication and
  frontend contract/error handling separately. Their static review is recorded
  separately from the controller's executed tests.

No test used a real app secret or OAuth flow, or contacted a live Feishu endpoint,
Hub service, or provider. Production remained on `a0e4a6174f28781dec9082da333e7173495b0255`, with
its pre-existing `start.sh` and `frontend/pnpm-workspace.yaml` state preserved.
