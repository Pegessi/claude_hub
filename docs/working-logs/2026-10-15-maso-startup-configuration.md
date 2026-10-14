# MASO URL as a service startup setting

Task `55034712-6188-44c2-aa02-b2edd1e3d462`, initiator-controlled with reviewed
policy retained. Follow-up to the completed local discovery work: the user asked
whether the MASO URL could be a Hub startup configuration item and what startup
configuration already exists.

The existing backend `Settings` reads process environment and `.env` in the
backend working directory. `start.sh` uses that directory. Listening host/port
remain launcher/script environment options; CLI connection TOML and session
environment presets have separate owners. There is no unified startup settings
page. This change reuses the backend configuration owner for MASO.

Worktree `~/claude_hub_worktree/maso-endpoint-discovery`, branch
`codex/maso-endpoint-discovery`, follow-up base
`058380d691368215b24ea970b15d8e10e4ae34f4`. Main/origin still matched
`2a43b616c65ce6dd15c78633ff48d7a4cea14883` after fetch; unrelated dirty main
state was preserved. No actual main `.env`, service, existing Chat, merge or push
is changed by this work.

## Changes

- Declare optional `Settings.maso_base_url`, accepting `MASO_BASE_URL` from the
  existing service environment/file configuration. Absent/empty permits discovery.
- Share one configured-URL lookup between startup diagnostics, preflight and
  creation: Chat URL > process URL > service Settings/file URL > process port >
  automatic local discovery. A file URL is explicit intent and never falls back
  when unavailable. Persisted Chat URLs retain priority after configuration edits.
- Validate the URL when readiness is checked so a malformed MASO URL can produce
  the existing nonfatal startup diagnostic without blocking other providers.
- Correct the local template/file documentation: `start.sh` reads `backend/.env`;
  Docker Compose injects the file selected by its own `env_file`. No credentials
  or machine-specific live configuration are committed.
- Extend the installed Claude Hub skill's startup reference to describe persistent
  configuration, precedence and the check for support in the current checkout.
  Older main must continue to use process environment until this code is integrated.

## Verification

Focused runner: 85 passed, 4 warnings, including real temporary dotenv loading,
startup/preflight/create consistency, environment/Chat overrides, explicit failure,
malformed URL redaction/nonfatal startup and empty-setting discovery. Evidence:
`/tmp/claude-hub-verify.7WhYG1/pytest.log` and
`.agent_office/maso-endpoint-discovery/verify-startup-config-focused.log`.

Independent read-only review passed with no actionable findings. Independent
discovery regressions: 23 passed, evidence
`/tmp/claude-hub-verify.2jEScR/pytest.log`.

Fresh-process probes against actual MASO passed file URL loading, explicit failed
file URL refusal and process URL override of the failed file URL. Successful
probes returned `http://127.0.0.1:11004`, 8 models. The probe's own temporary cwd
and `.env` are under task artifacts; evidence is
`.agent_office/maso-endpoint-discovery/live-startup-config.json`. These checks
made only read-only model catalog requests, with private Hub runtime overrides.

The installed skill reference is byte-identical to the reviewed candidate and
passes the skill validator; its prior reference was retained as
`.agent_office/maso-endpoint-discovery/maso-startup.before-config.md`. The cumulative
portable skill patch is updated in the same branch.

Final `./scripts/verify.sh all`: exit 0, all nine standard targets passed.
Backend behavior: 3308 passed, 7 skipped (explicit browser opt-in), 33 warnings;
backend format/types and all frontend lint/types/unit/build checks passed. Evidence:
`.agent_office/maso-endpoint-discovery/verify-startup-config-all.log`.

Skill validator and cumulative patch apply/readback passed. Test configuration
files live in private pytest or task-artifact directories; no `.env` was placed
in the feature backend or primary checkout.

## Delivery limits

The feature branch includes the preceding discovery commit. It is not integrated
or deployed, and the shared Hub was not restarted. File changes are startup
configuration, not hot reload; saved Chats keep their own endpoints. Checks do
not establish upstream inference credentials/quota or real browser creation.
