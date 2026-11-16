# 2026-11-16 The two MASO rows that stayed "Unlisted endpoint"

## Symptom

The provider-quota panel listed `[redacted]`, `[redacted]`, `[redacted]` and
`[redacted]` by name, but two rows read
`Unlisted endpoint · ktzs4` (66% left) and `Unlisted endpoint · lmdcb` (97%
left). Reported by the user: those two rows are models they use, not gateway
keys.

## Where the id comes from

`/api/llm-quota` is Code Router's ledger, forwarded by MASO. Router keeps one
ceiling **per upstream deployment**, and names the row with that deployment id:

```
$ curl -s localhost:11004/api/llm-quota | jq -r '.models[].model'
[redacted]      [redacted]
[redacted]      [redacted]
[redacted]      [redacted]
[redacted]      openai/qwen3.8-27b
```

The Hub names such a row from `/api/models`, matching the deployment against a
catalog row's `model_name` (2026-11-12 working log). That works for six of the
eight rows; the two endpoints Router created on 2026-11-12 are absent from the
catalog, so the previous work labelled them for what they were and recorded
"naming unlisted endpoints properly still needs MASO to publish a name for
them".

They are not stubs. Router's model config pins models to them:

```
$ curl -sX POST https://[redacted]/api/v1/zti-authorize \
    -H "Authorization: Bearer $(cat [redacted]/identity.token)" \
  | jq -r '.providers[] | select(.model|startswith("[redacted]")) | "\(.model)\t\(.name)\t\(.client_type)\t\(.provider)"'
[redacted]  [redacted]                AzureResponsesAPI  pioneer-inhouse-ark
[redacted]  [redacted]_reasoning_high AzureResponsesAPI  pioneer-inhouse-ark
[redacted]  [redacted]                  AzureResponsesAPI  pioneer-inhouse-ark
[redacted]  [redacted]_reasoning_max    AzureResponsesAPI  pioneer-inhouse-ark
[redacted]  [redacted]                AzureResponsesAPI  pioneer-inhouse-ark
[redacted]  [redacted]             AzureResponsesAPI  pioneer-inhouse-ark
```

So `ktzs4` is the deployment behind `[redacted]`, and `lmdcb` is one shared
in-house deployment behind `[redacted]`, `[redacted]` and `[redacted]`; the
ledger keeps one ceiling per deployment, not one per model. MASO's own logs
agree independently — every 429 during a `[redacted]` turn names the
deployment:

```
[Agent] MASO (id=da55587f-d17) LLM call failed on turn 171: RateLimitError:
Error code: 429 - {'error': {'code': 'rpm_rate_limited', 'message': 'Request
rate limit exceeded for this key on model "[redacted]". ...
```

## Why the catalog did not answer

`/api/models` names `[redacted]` with `model_name: "[redacted]"` — the model
MASO sends, which the proxy resolves to the deployment. That is the Router
config as MASO loaded it, before the 2026-11-12 pinning; the running process
serves that snapshot, so the deployment ids appear nowhere in it. Router's
quota payload does not carry a name either (`date`, `apiKeyName`, `models[]`
with only `model`/state/percentages/`resetAt`), and the Hub may not read
Router's credentials, so no surface the Hub is allowed to read pairs the
deployment with a model.

Two consequences worth keeping in mind:

- MASO's own quota strip does not show these rows at all: it buckets quota rows
  by catalog `model_name`, so a row for a deployment no catalog key names is
  dropped. The Hub shows more than MASO does here, which is why the gap is
  visible in the Hub first.
- A MASO process that reloads this config would make `/api/models` carry the
  deployment ids, and the existing catalog mapping would name both rows without
  any help from the table below.

## Fix

`maso_http.pinned_deployment_labels()` is a small table of deployments Router
pinned after this catalog was built:

```python
{
    "[redacted]": "[redacted]",
    "[redacted]": "[redacted] · [redacted]",
}
```

`read_maso_quota` seeds its endpoint-label map with it and then lets every
catalog row override the seed, so:

- the two rows read `[redacted]` and `[redacted] · [redacted]` — every
  selectable model the one ceiling covers, joined the way MASO's own quota rows
  join grouped variants (`[redacted]` is left out because this catalog has no
  such key to select);
- `scope` still keeps the deployment, so the row tooltip reads
  `[redacted] ([redacted])`;
- a catalog that names the deployment wins outright, so the table retires
  itself once MASO reloads, without a code change;
- a deployment id in neither source keeps `Unlisted endpoint · <code>`, so a
  rotated deployment under-reports instead of inheriting a stale name. That is
  the failure to prefer: a wrong model name on a percentage is worse than an
  opaque code.

The table is in `maso_http` next to `endpoint_labels`, with the command that
re-derives it in the comment: it is a fact about this Router's config, not a
second model identity.

## Limits that remain

- Rotating a deployment (a new `ep-…` id for the same model) makes its row
  unlisted again until the table is updated. Nothing in the Hub can notice the
  rotation: Router states no name and the pinned model still resolves through
  the proxy.
- If MASO's catalog and the table ever disagree about the same deployment, the
  catalog wins by construction, so the panel can show a different name than the
  table without any warning. The disagreement is deliberate — the catalog is
  live and the table is the stopgap.
- The real fix is upstream: a deployment-keyed ledger should publish the model
  it serves (or MASO should name rows from the config that pins them), at which
  point `pinned_deployment_labels` can be deleted.

## Validation

- `backend/tests/test_provider_quota.py`: a quota row naming `ktzs4` is labelled
  `[redacted]` with the deployment kept in `scope`, and the reading still asks
  the raw catalog once, so a catalog that does name it would have won; a row
  naming `lmdcb` is labelled with both models it serves; a catalog row naming
  `ktzs4` keeps its own label; `pinned_deployment_labels()` returns a copy, so
  one reading cannot rename another's deployments.
- `frontend/tests/quotaMenu.test.mjs`: a row labelled with two model names keeps
  every name and appends the deployment in its tooltip.
- Live read against `127.0.0.1:11004` with the fixed code: the two rows report
  `[redacted] ([redacted], 35% used)` and
  `[redacted] · [redacted] ([redacted], 4% used)`.
- `./scripts/verify.sh all` for the rest.
