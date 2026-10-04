# Five-layer ownership and bounded agent context

## System overview

The five layers are a map of existing responsibilities: intent (Task/Goal Packet),
context (derived prompts/snapshots), execution (sessions/dispatch), verification
(reports/reviewer/evaluator), and governance (runtime boundaries/lifecycle/feedback).
The map is in `ARCHITECTURE.md`; it creates no new state store or control plane.
The source design discussion is the [Agent OS document, revision 8](https://bytedance.larkoffice.com/docx/GuXNdeQvEoyHRBx1aNUcpVMJngb).

Before this change, simple autonomous assignments said both “execute directly” and
“must spawn P-JUDGE”, then globally prohibited execution/validation in the owner
context. Complex assignments prescribed fixed role counts and Claude model tiers,
and continuation/recovery always demanded orchestration. Reviewer instructions
could reject honest serial execution because no subagent ledger existed. These
contradictions made process compliance compete with completion evidence.

## Module design

- `_prompts.py` keeps the existing task modes, report schema and independent Hub
  evaluator gate. Simple tasks receive a compact direct-execution contract;
  complex tasks map dependencies and delegate where isolation is useful. Auto
  records its chosen strategy in Goal Packet assumptions. Roles describe work,
  not a minimum count of agents.
- Delegation names an owner, inputs/base/head, dependencies, allowed paths and
  read/write boundary, resources, budget, stop condition and evidence handoff.
  Only actual delegations need a ledger. The owner integrates and checks results;
  mechanical checks need no extra LLM. User/configured model choices take
  precedence over role heuristics, with actual runtime evidence or honest limits.
- Six executable/report examples emitted an extra right brace because ordinary
  string suffixes used f-string escaping. The new schema check first reproduced
  `JSONDecodeError: Extra data` on the generated subagent started report. Worker,
  subagent, reviewer and ACK examples now emit valid JSON; nested Goal Packet
  braces remain intact. Tests validate the final payloads with `AgentReportCreate`.
- Subagent-mode assignments stay lightweight and keep caller-owned acceptance.
  Their completion example uses existing `validation` and `risks` fields for
  reproducible evidence and unverified criteria.
- Assignment and recovery explicitly treat snapshots as navigation caches. Current
  Task/report records and Git determine facts. Recovery checks whether the prior
  report persisted before retrying with its call_id; a stale ready-for-review
  summary no longer instructs the worker to post completed automatically.
- Review starts with requirements, the candidate checkout/diff and call paths.
  Existing fields carry cwd/base/head, commands/results, artifact paths, causal
  findings and uncertainty. Prompt edits do not weaken backend review transitions.
- Root `AGENTS.md`/`CLAUDE.md` remain byte-identical and shrink from 257 to 107 lines.
  Operational details and the full prior task-specific navigation table move to
  `docs/AGENT_WORKFLOW.md`; hard worktree/runtime/ownership boundaries stay at root.
- `scripts/measure_prompts.py` now inserts this checkout's backend path and creates
  an owned temporary home/state root/tmux socket **before imports**. It overrides
  inherited runtime settings and verifies the prompt module's source. Its
  temporary directory is removed on exit. The former `scripts/backend` path and
  unused `CLAUDE_HUB_DATA_DIR` did not guarantee isolated checkout-local loading.

## Validation and prompt-size observations

Commands use an existing Python environment with this worktree's backend first
on `PYTHONPATH`; `/opt/homebrew/bin` supplies tmux for isolated pytest cleanup.
Tests exercise final generated assignment/review/continue/cold-recovery/revision
prompts across simple/complex/auto and Claude/Codex/terminal runtimes, validate the
subagent report example against the real report schema, and run the measurement
script from an unrelated cwd with deliberately conflicting inherited settings.
Existing state-policy tests cover the review gate and autonomous evaluator routing.

Measurement uses the same synthetic fixture before and after. Characters are
stable; tokenizer counts can vary slightly with synthetic IDs and temporary paths.
These are prompt sizes, not measured latency, cost or completion-quality gains.

| Generated prompt | Before chars | After chars |
| --- | ---: | ---: |
| Autonomous simple assignment | 8,945 | 7,638 |
| Autonomous complex assignment | 9,152 | 9,013 |
| Autonomous continuation | 1,353 | 1,245 |
| Cold recovery | 2,374 | 2,591 |
| Revision recovery | 1,960 | 2,755 |

Simple assignment is 14.6% shorter. Recovery is deliberately longer because it
now carries source-of-truth, strategy and report-replay boundaries. The root guide
is 58.4% shorter; the details remain discoverable in the linked reference.

Final checks:

- 125 prompt/measurement/entry-doc/CLI regression tests passed; one known baseline
  remote-profile fixture failure was deselected after it reproduced independently
  at the base SHA (`object()` lacks `interactive` in `test_cli_reuse_lifecycle`).
- 101 workspace-state-policy tests passed.
- Black/isort with `backend/pyproject.toml`, targeted mypy, `git diff --check`, root
  guide parity and local Markdown link resolution passed.
- A broader subagent/API attempt was interrupted after 5 passing cases in 154.9s;
  the remaining API cases were not validated by that attempt. Only the owned pytest
  process received SIGINT and exited; the live Hub was untouched.

No live Hub task, provider session, main-service restart or real model trial was used. Prompt/API
regressions establish contract consistency; they do not prove improved agent
success rate or production throughput.

## Pitfalls and directory hygiene

An initial targeted pytest run passed every test body but its cleanup could not
find tmux on PATH. Explicitly adding the known Homebrew bin directory fixes the
environment; no product code change is needed.

A bounded inventory read only the primary checkout's top level and Git worktree
metadata. It found one registered historical checkout outside the canonical root
and protected logs/probes/task media at the primary root. No history was moved or
deleted. Being old/merged is insufficient proof of disposability: dirty/untracked
and needed ignored files, process/tmux/server/browser usage and ownership must be
checked first. A separate inventory/cleanup change can handle those candidates.
