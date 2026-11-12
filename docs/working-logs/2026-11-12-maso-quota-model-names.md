# 2026-11-12 MASO quota rows name models, not endpoints

## Symptom

The provider-quota panel listed MASO models as opaque endpoints
(`ep-20260815235131-vfr5s`, `ep-20261105102229-nfbn5`) instead of the model names
people pick from the model menu (`seed-stable`, `deepseek-v41-flash`). Reported
by the user: `Quota展示，maso很多模型展示的是id，而不是模型name`（不太对）.

## Evidence

Live MASO service on `127.0.0.1:11004` while the panel showed ids:

```
$ curl -s localhost:11004/api/llm-quota | jq '.models[0]'
{"model":"ep-20260815235131-vfr5s","state":"no_limit", ...}
```

`/api/llm-quota` names its rows by the upstream endpoint (`model`), and has no
display-name field. The catalog that already feeds the model picker knows both
identities:

```
$ curl -s localhost:11004/api/models | jq '.[0]'
{"key":"seed-stable","display_name":"seed-stable",
 "model_name":"ep-20260815235131-vfr5s","selector_primary_key":"seed-stable"}
```

Eleven endpoints back thirty-six selector keys: `seed-stable`,
`seed-stable-reasoning` and `seed-stable-reasoning-high` share one endpoint, and
`selector_primary_key` names the group (`seed-stable`).

`backend/claude_hub/services/provider_quota.py` (`_maso_limits`) set
`label=model`, so the panel had no other name to show.

## Fix

- `backend/claude_hub/services/provider_quota.py` is the whole behaviour change.
  `_maso_limits(rows, labels)` labels a row with the catalog name for its
  endpoint and keeps `scope=model`, because the endpoint is what MASO enforces and
  what a grant would name. `read_maso_quota` builds the map from the options
  `discover_maso_service` already returned for this reading — discovery fetches
  `/api/models` on every read, so no extra request is made and a reading still
  costs one quota call. The map is first-wins per endpoint, so a catalog that
  repeats an endpoint without a shared primary marker keeps the first name.
- `backend/claude_hub/services/agent_stream/maso_http.py` gains no function: it
  only carries a comment pinning the contract the mapping relies on.
  `_model_options` already sets `StreamModelOption.description` to the row's
  `model_name` (the upstream endpoint quota rows name) and `label`/`id` to
  `selector_primary_key or key` (the model people select), collapsing the
  reasoning variants into one option per group.

Deliberately unchanged: the model picker (`StreamModelOption.label = key` was
already a name), and the quota cache TTL — the label lookup runs with the
snapshot, so a cached reading makes no upstream call.

## Limit that remains

Three of the nine endpoints MASO reported quota for are absent from
`/api/models` (`ep-20261020185502-b8c6q`, `ep-20261105102229-nfbn5`,
`ep-20261105102502-xpjfn`): MASO enforces quota for endpoints it does not list
as selectable models, so no name exists for them in any surface the Hub may read
(`/api/model-health` uses the same ids; `/api/models` ignores filter parameters
and returns the same thirty-six listed rows; the Hub may not read MASO's own
database). A name cannot be invented, so those rows say what they are —
`Unlisted endpoint · <code>`, from the endpoint id's trailing code so two of them
differ at a glance (two that share a code keep their full ids instead), and
`scope` keeps the endpoint itself. The quota panel's label tooltip now shows
`label (scope)` whenever the two differ (`frontend/src/utils/quotaDisplay.ts`,
`QuotaMenu.vue`), so the full id is one hover away. Known limit: that tooltip is
a native `title`, so a touch or keyboard-only reader sees the short label without
the id; the panel is a passive display and the label is self-describing, but the
id would need a visible secondary line to be reachable without a mouse. Naming
unlisted endpoints properly still needs MASO to publish a name for them.

## Validation

- `backend/tests/test_provider_quota.py`: the MASO fixture's discovery returns
  catalog options; the reading asserts `label == "seed-stable"` while `scope`
  stays `ep-20260815235131-vfr5s`, an endpoint missing from the catalog keeps its
  id, and a test pins the catalog contract the mapping relies on (one option per
  selector group, `label` = primary key, `description` = endpoint, deprecated and
  variant rows collapsed). Call counts stay at one quota request per reading and
  none when the reading is cached.
- Live check against the running service: 6 of 9 rows now carry model names
  (`seed-stable`, `seed-nightly`, `gpt55`, `deepseek-v41-flash`, `gemini-4-argon`,
  `ada-qwen-27b-superfast`); the three endpoints MASO does not list read
  `Unlisted endpoint · <code>` with the endpoint kept in `scope` (above).
- `frontend/tests/quotaMenu.test.mjs`: a row whose label describes the limit
  keeps `label (scope)` in its tooltip; `browser_service_restart.py` opens the
  real panel with an `Unlisted endpoint · b8c6q` / `ep-...` fixture and asserts
  both the rendered label and its `title` (the backend naming itself is pinned by
  the backend tests, because that scenario serves a mocked payload).
- `./scripts/verify.sh backend-format`, `backend-types`, `docs`, the backend
  quota suite, the frontend unit suite (1484) and the restart browser scenario.
