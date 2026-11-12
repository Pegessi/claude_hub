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

## 2026-11-11: the approval gate is opt-in

The mechanism above is unchanged; its default is not. A Goal started to
implement a small review fix parked on `blocked` and rendered
"Approve & start / Revise" without anyone asking to review a plan — the setup
dialog's Plan-first checkbox had simply been ticked because it opened ticked.

- `readPlanFirstPreference` returns `false` when nothing is stored, and
  `GoalSetupDialog` opens unchecked and says `The Goal starts working immediately.`
  Ticking it shows the old promise (`Nothing starts until you approve.`) and
  sends the same additive `plan_first: true`.
- The preference key is now `claude-hub.goal-plan-first.v2`. The old key was
  written on every Goal creation, so a stored `true` there means "the dialog
  was on by default", not "the user wanted a gate"; carrying it over would keep
  the surprise for existing users. A deliberate opt-in is remembered from now
  on under the new key.
- Unchanged: the plan envelope, the `needs_input` park, the plan card and its
  actions, the refusal for provider-native Goals, and every backend contract.
  A caller that sends `plan_first: true` still gets exactly the approval flow
  described above; a caller that omits it (CLI, Feishu, older clients) is
  untouched.
- Validation: `frontend/tests/goalPlanCard.test.mjs` (default off, round trip,
  and the legacy key ignored), `frontend/tests/browser_chat_goal_steer.py` (the
  dialog opens unchecked, sends `plan_first: false`, and gates after ticking —
  the plan-card assertions still run through the opt-in path).

Not done here: a plan that is *displayed* without gating. That would need a
third state (produce the plan, keep working, show it as an aid) and a new
request/goal field, which the design deliberately avoids inventing; ask before
adding one.
