# 2026-11-09 Goal plan-first scope and execution-budget approval

Design note for the additive Goal scope approval (`plan_first`).

## Problem

A Goal dispatched an implementing turn the moment the user submitted an
objective. Long-running Goals therefore started helper agents, chose models and
took actions before the user had seen the scope or the cost. The user's
standing rule is that nothing autonomous starts without consent, and the Hub
is not allowed to invent new control layers for it.

## Shape of the change

- `GoalRunCreate.plan_first` is additive and optional. A caller that never sends
  it gets the previous continuation prompt byte for byte and the same
  start-immediately behaviour, so CLI, Feishu and other clients are unchanged.
- With `plan_first`, the *first* Goal turn receives a plan envelope instead of
  the continuation envelope. It may only plan: a prose scope summary, numbered
  steps with acceptance checks, and an execution budget (helper count,
  provider+model per helper, maximum parallelism, rough duration). It must take
  no implementing actions and end with the existing
  `<goal-status state="needs_input">`, which the controller already turns into
  `blocked`. No new state machine: approval is the user's next message on the
  existing resume/steer path, which already continues a blocked Goal.
- The response may carry one machine-readable `<goal-plan>` JSON block. It is
  parsed against a bounded contract (`GoalPlan`, `extra="forbid"`), stripped
  from published assistant text by the existing Goal protocol sanitizer, and
  stored beside the existing checkpoint. A malformed block is reported as
  `plan_warning` and never authorizes anything.
- `plan_turn_id` keys the planning turn in the Goal snapshot, so the UI
  identifies the card from control-plane identity rather than by matching prose.
- A plan block is accepted only from the turn the Hub asked to plan, or from a
  turn that ends by parking for the user (`needs_input`, which is how a revision
  asks again). An implementing turn that merely echoes the plan therefore
  cannot rewrite the approved scope or re-open its approval.
- The pause is the Hub's, not the model's: a turn that produced a plan and has
  not been answered parks as `blocked` even when the model asks to `continue`,
  so nothing implements itself. Only `complete` ends a planning turn without a
  pause. If the objective is edited while the planning turn runs, the
  replacement turn re-plans instead of implementing the scope the user
  replaced.
- A card in `review` with no validated plan refuses approval with a reason
  (Revise stays available, since that is the way out), so a request for input
  without a reviewable plan cannot start work.
- The trailing-envelope sanitizer (ordinary turns in a Goal-using Chat)
  recognises an optional leading `<goal-plan>` block too, so a plan block never
  leaks into a durable completion summary.
- `plan_answered` records that a turn carrying the user's own message was
  dispatched after the plan existed; that is what ends the approval request.
  It deliberately does not mean "approved": the Hub cannot tell approval from a
  change request without reading the user's prose, so the card says only that
  the Goal continued.
- The remembered "Plan first" choice is written only by a Chat that can offer
  the option, so one provider-native Goal cannot turn it off everywhere. Each
  card's heading id comes from its turn id, so two cards never share one
  accessible name.
- Provider-native Goals never accept a Hub turn, so `plan_first` is refused for
  them (409) and the setup dialog disables the option with the reason.

## Verification

- `backend/tests/test_goal_plan.py` covers the plan envelope, the needs_input
  park, approval switching to the continuation prompt, revision, cancelled
  planning turns, invalid plans, unchanged non-`plan_first` behaviour, the
  refused native creation and the visible-text sanitizing.
- `frontend/tests/goalPlanCard.test.mjs` covers turn identification, action
  gating, the fixed protocol messages, the budget summary and the remembered
  preference (including a blocked `localStorage`).
- `frontend/tests/browser_chat_goal_steer.py` renders the setup option and the
  plan card in the real SPA with mocked APIs at 1440×900 and 390×844, and
  asserts that Revise sends nothing while Approve sends exactly the approval
  message.

## Known limits

- The plan block sits at most one goal-checkpoint window (64 KiB + 4 KiB) from
  the end of the turn; a plan larger than that window would be missed rather
  than misread.
- An ordinary (non-Goal) turn that emits `<goal-plan>` in prose keeps it visible:
  only Goal turns strip every control block.
