# Unified model and thinking-effort menu

Date: 2026-09-17 · Branch: `feat/model-effort-menu`

## System overview

Structured Chat now presents model selection as one provider-neutral control.
The compact trigger shows `Model · Effort`; opening it reveals a searchable
model list and a second panel containing only the selected model's supported
thinking levels. On narrow screens the panels stack vertically.

## Provider normalization

- Codex and TraeX expose `model/list` through their shared app-server protocol.
  Hub uses its model label, description, default effort, and supported efforts.
  A selected effort is stored as `CODEX_REASONING_EFFORT` and sent through
  `collaborationMode.settings.reasoning_effort` on the next turn. The legacy
  `TRAEX_REASONING_EFFORT` key remains readable for existing tabs.
- Cursor encodes effort in the model ID itself. The runtime catalog parser
  groups variants such as `gpt-5.6-sol-low` and `gpt-5.6-sol-high` under one
  `gpt-5.6-sol` UI model. Each effort retains its exact provider model ID, so
  selection still reaches Cursor through the existing `--model` launch flag.
- Models without effort metadata remain one-click choices. Custom model IDs
  remain available at the bottom of the menu.

## Key pitfalls

`collaborationMode.settings` overrides top-level Codex-family turn settings, so
reasoning effort must be placed there. Capability polling also runs the base
static-catalog loader; the provider-discovered catalog is cached on the session
and restored after later polls so effort metadata is not lost.

Cursor display grouping must never synthesize a launch ID. The normalized
schema therefore carries `provider_model_id` both for a model's default path and
for each effort option.
