# 2026-11-18 Preempting background wake/scheduled turns on user Feishu messages

## Symptom

At 00:00 CST, a scheduled `chat_turn` (`Disk 90% alert`, schedule `03688bfd`) started on the Dot Chat tab (`7958b4d0`) and ran until 00:08:35. When the user sent two consecutive Feishu messages at 00:09, the first message hit HTTP 409 `chat_busy` (`"a turn is already in flight"`) and replied `"Claude Hub Chat 正在处理其他消息或等待网页回答，本条消息尚未执行。"`.

## Root causes

1. **Narrow preemption scope in `yield_dot_turn`**: `yield_dot_turn` only checked `service.is_system_turn(active)`, which only matched Dot's own `wake_schedule_id` and `tidy_schedule_id`. Other scheduled `chat_turn`s on Dot or on ordinary bound Chat tabs were not preempted when a user Feishu message arrived.
2. **Handoff race during rapid consecutive Feishu messages**: On Dot, when message `A2` arrived while `A1` had set `native_bridge_started = True` in `_dispatch_tab_batch` and was inside `_admit_batch_dispatch` / `dispatch_tab_stream_input` preempting a background turn, `_interrupt_dot_answer` immediately spawned `_stop_superseded_answer(expected_turn_id=turn_id_for(A1))`. Because `A1`'s turn ID had not yet been set on the tailer, `cancel_turn` either returned `False` or matched `_turn_teardown_pending` from the background turn's teardown without checking `expected_turn_id`.
3. **Scheduler insertion during Feishu coalescing**: While a Feishu batch waited in its 1–5s quiet window or transitioned between a superseded turn and the next batch, `ChatMessageInbox` could dispatch a queued scheduled `chat_turn`.
4. **Window before `_send_lock`**: If a background turn entered flight between `preempt_background_turn` and `tailer.send_message` acquiring `_send_lock`, `InputMixin.send_message` rejected the user's Feishu message with `RuntimeError("a turn is already in flight")`.

## Fix

1. **Route-level background preemption (`api/routes/agent_stream/dispatch.py`)**:
   - Added `_is_background_turn` and `preempt_background_turn`, invoked from `dispatch_tab_stream_input` when `origin == "feishu"` or `not allow_question_answer`.
   - Classification strictly checks trustworthy scheduled/system provenance (`service.is_system_turn`, `workspace_manager.scheduled_task_runs`, `ChatMessageInbox` scheduled envelopes, tailer `_active_turn_metadata` with `yields_to_user` or `origin in {"scheduled_task", "wake", "tidy"}`, or `scheduled-*` turn IDs when origin is unset).
   - Ordinary user turns (`origin in {"web", "feishu", "hub_message"}`) always take precedence and are never classified as background even if user-supplied text starts with `[Dot wake]` or `[Dot tidy]`.
2. **Atomic in-lock preemption (`services/agent_stream/runtime/input.py`)**:
   - Tracked `_active_turn_metadata` on `SessionState`. Inside `InputMixin.send_message` under `_send_lock`, if `transport.turn_in_flight` holds a background turn and `incoming_origin == "feishu"`, the tailer cancels the background turn inline with `await_teardown=True` before delivering the user's Feishu turn.
3. **Turn-scoped teardown & handoff barrier (`retirement.py`, `feishu_bot.py`, `message_dispatch.py`, `queueing.py`)**:
   - Tracked `_teardown_turn_id` on `SessionState` so `cancel_turn(expected_turn_id=...)` awaits an unrelated turn's teardown instead of short-circuiting to `True`.
   - Added `native_handoff_pending` and `native_handoff_done` on `TabDispatchItem` so `_stop_superseded_answer` waits for the active batch's `_admit_batch_dispatch` guard to complete before calling `manager.cancel_turn`.
4. **Scheduler deferral while Feishu dispatches (`feishu_bot.py`, `chat_runs.py`, `messages.py`)**:
   - Added `feishu_dispatch_in_progress(tab_id)` so scheduled `chat_turn`s wait while a Feishu queue has pending/active/processor items, and woke the inbox when `_promote_tab_dispatch` drains the queue.
