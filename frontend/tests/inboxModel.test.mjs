/**
 * Inbox model + inline reply contract.
 *
 * The grouping rules decide what the top bar's badge means, so they are pinned
 * here as behaviour rather than as implementation details: a pending question,
 * an unread finished turn and a failure all "need you"; a running turn does not;
 * and everything else is history.
 */
import assert from 'node:assert/strict'
import test from 'node:test'
import { createSidebarLoader } from './chatSidebarTestHarness.mjs'

const load = createSidebarLoader()
const {
  buildInbox,
  classifyRow,
  flattenRows,
  formatElapsed,
  groupRowsByWorkspace,
  keyTargetKind,
  moveSelection,
  needsYouCount,
  RECENT_DONE_WINDOW_MS,
  resolveInboxKey,
  statusLabel,
} = await load('features/agent-status/inboxModel.ts')
const { buildInboxReplyBody, canReplyInline, inlineReplyPath, sendInboxReply } = await load(
  'features/agent-status/inboxReply.ts',
)

const NOW = Date.parse('2026-11-09T12:00:00Z')

function tab(overrides = {}) {
  return {
    id: 'tab-1',
    name: 'Session',
    session_kind: 'chat',
    agent_type: 'claude',
    created_at: '2026-11-09T00:00:00Z',
    is_active: true,
    ...overrides,
  }
}

function status(overrides = {}) {
  return {
    tab_id: 'tab-1',
    tab_name: 'Session',
    agent_type: 'claude',
    status: 'idle',
    status_text: 'Idle',
    tmux_session: 'tmux-1',
    sampled_at: '2026-11-09T11:59:00Z',
    ...overrides,
  }
}

function only(groups, key) {
  return groups.find(group => group.key === key)?.rows ?? []
}

test('a pending question is the top reason to show the inbox at all', () => {
  const row = classifyRow(tab(), status({ status: 'working', pending_question: true }), NOW)
  assert.equal(row.group, 'needs')
  assert.equal(row.reason, 'Waiting for your answer')
})

test('a blocked question keeps a timestamp even without an activity clock', () => {
  // An idle turn that asked a question reports no activity_since; the newest
  // reply is its only real time, so the top row must still say when.
  const withActivity = classifyRow(
    tab(),
    status({ status: 'working', pending_question: true, activity_since: '2026-11-09T11:52:00Z' }),
    NOW,
  )
  const replyOnly = classifyRow(
    tab(),
    status({ pending_question: true, activity_since: undefined, last_reply_at: '2026-11-09T11:48:00Z' }),
    NOW,
  )
  const empty = classifyRow(tab(), status({ pending_question: true }), NOW)
  assert.equal(withActivity.sinceMs, Date.parse('2026-11-09T11:52:00Z'))
  assert.equal(replyOnly.sinceMs, Date.parse('2026-11-09T11:48:00Z'))
  assert.equal(replyOnly.group, 'needs')
  assert.equal(empty.sinceMs, null)
})

test('an unread finished turn needs you, a read one does not', () => {
  const unread = classifyRow(tab({ is_unread: true }), status({ last_reply_at: '2026-11-09T11:50:00Z' }), NOW)
  const read = classifyRow(tab({ is_unread: false }), status(), NOW)
  assert.equal(unread.group, 'needs')
  assert.equal(unread.reason, 'New reply')
  assert.equal(read.group, 'done')
})

test('a still-running turn with an older unread reply stays in Working', () => {
  const row = classifyRow(
    tab({ is_unread: true }),
    status({ status: 'working', activity: 'Running "pytest -q"', activity_since: '2026-11-09T11:59:00Z' }),
    NOW,
  )
  assert.equal(row.group, 'working')
  assert.equal(row.reason, 'Running "pytest -q"')
  assert.equal(row.sinceMs, Date.parse('2026-11-09T11:59:00Z'))
})

test('attention and agent offline rows need you, dead shell tabs do not', () => {
  const attention = classifyRow(tab(), status({ status: 'attention', detail: 'Last turn failed (error)' }), NOW)
  assert.equal(attention.group, 'needs')
  assert.equal(attention.reason, 'Last turn failed (error)')

  const agentOffline = classifyRow(tab(), status({ status: 'offline', detail: 'Agent is offline' }), NOW)
  assert.equal(agentOffline.group, 'needs')

  const shell = classifyRow(
    tab({ session_kind: 'terminal', agent_type: undefined, is_active: false }),
    status({ status: 'offline' }),
    NOW,
  )
  assert.equal(shell.group, 'done')
})

test('working rows prefer the concrete activity over the state sentence', () => {
  const withTool = classifyRow(tab(), status({ status: 'working', activity: 'Reading "src/app.ts"' }), NOW)
  assert.equal(withTool.reason, 'Reading "src/app.ts"')
  // The state sentence is not a preview: a running row shows one line.
  assert.equal(withTool.excerpt, null)
  const bare = classifyRow(tab(), status({ status: 'working', activity: null, detail: 'Responding' }), NOW)
  assert.equal(bare.reason, 'Responding')
})

test('a preview that only repeats the reason is dropped', () => {
  const row = classifyRow(
    tab(),
    status({ status: 'attention', detail: 'Last turn failed (error)', last_reply_excerpt: 'Last turn failed (error)' }),
    NOW,
  )
  assert.equal(row.reason, 'Last turn failed (error)')
  assert.equal(row.excerpt, null)
})

test('the badge counts needs-you rows only', () => {
  const tabs = [
    tab({ id: 'a', name: 'A', is_unread: true }),
    tab({ id: 'b', name: 'B', is_unread: false }),
    tab({ id: 'c', name: 'C', is_unread: false }),
  ]
  const statuses = [
    status({ tab_id: 'a' }),
    status({ tab_id: 'b', status: 'working', activity: 'Responding' }),
    status({ tab_id: 'c' }),
  ]
  const groups = buildInbox(tabs, statuses, { nowMs: NOW })
  assert.equal(needsYouCount(groups), 1)
  // ...and the same model still lists the other two somewhere else.
  assert.equal(only(groups, 'working').length, 1)
  assert.equal(needsYouCount([]), 0)
})

test('stale finished rows drop out of Recently done', () => {
  const old = Math.floor((NOW - RECENT_DONE_WINDOW_MS - 60_000) / 1000) * 1000
  const rows = buildInbox([tab({ id: 'old' })], [status({ tab_id: 'old', last_reply_at: new Date(old).toISOString() })], {
    nowMs: NOW,
  })
  assert.equal(only(rows, 'done').length, 0)
})

test('the manual/agent filter is a filter, not a second list', () => {
  const tabs = [tab({ id: 'm', name: 'Manual' }), tab({ id: 'w', name: 'Workspace', workspace_id: 'ws-1' })]
  const statuses = [status({ tab_id: 'm' }), status({ tab_id: 'w' })]
  assert.deepEqual(
    flattenRows(buildInbox(tabs, statuses, { nowMs: NOW, scope: 'manual' })).map(row => row.tab.id),
    ['m'],
  )
  assert.deepEqual(
    flattenRows(buildInbox(tabs, statuses, { nowMs: NOW, scope: 'workspace' })).map(row => row.tab.id),
    ['w'],
  )
  assert.equal(flattenRows(buildInbox(tabs, statuses, { nowMs: NOW, scope: 'all' })).length, 2)
})

test('archived sessions never enter the inbox', () => {
  const rows = buildInbox([tab({ archived: true, is_unread: true })], [status()], { nowMs: NOW })
  assert.equal(flattenRows(rows).length, 0)
})

test('the Agents view groups rows under their workspace, sub-agents included', () => {
  const tabs = [
    tab({ id: 'a1', name: 'agent one', workspace_id: 'ws-a', workspace_name: 'Repo A' }),
    tab({ id: 'b1', name: 'agent two', workspace_id: 'ws-b', workspace_name: 'Repo B' }),
    tab({
      id: 'a2',
      name: 'child',
      workspace_id: 'ws-a',
      workspace_name: 'Repo A',
      parent_tab_id: 'a1',
      subagent_id: 'sa-1',
    }),
  ]
  const statuses = ['a1', 'b1', 'a2'].map(id =>
    status({ tab_id: id, status: 'working', activity: 'Responding' }),
  )
  const rows = flattenRows(buildInbox(tabs, statuses, { nowMs: NOW, scope: 'workspace' }))
  const sections = groupRowsByWorkspace(rows)
  assert.deepEqual(
    sections.map(section => [section.title, section.rows.map(row => row.tab.id)]),
    [
      ['Repo A', ['a1', 'a2']],
      ['Repo B', ['b1']],
    ],
  )
  // The sub marker is a property of the row, independent of grouping.
  assert.equal(sections[0].rows[1].tab.parent_tab_id, 'a1')
})

test('workspace grouping keeps one untitled section for rows without a workspace', () => {
  const rows = flattenRows(
    buildInbox([tab({ id: 'solo', name: 'solo chat' })], [status({ tab_id: 'solo' })], { nowMs: NOW }),
  )
  const sections = groupRowsByWorkspace(rows)
  assert.equal(sections.length, 1)
  assert.equal(sections[0].title, '')
  assert.deepEqual(sections[0].rows.map(row => row.tab.id), ['solo'])
})

test('the cursor clamps instead of wrapping past the ends', () => {
  assert.equal(moveSelection(-1, 1, 3), 0)
  assert.equal(moveSelection(-1, -1, 3), 2)
  assert.equal(moveSelection(0, -1, 3), 0)
  assert.equal(moveSelection(2, 1, 3), 2)
  assert.equal(moveSelection(1, 1, 3), 2)
  assert.equal(moveSelection(0, 1, 0), -1)
})

test('elapsed labels stay short and never read as negative time', () => {
  assert.equal(formatElapsed(null, NOW), '')
  assert.equal(formatElapsed(NOW - 5_000, NOW), 'just now')
  assert.equal(formatElapsed(NOW - 3 * 60_000, NOW), '3m')
  assert.equal(formatElapsed(NOW - 2 * 3_600_000, NOW), '2h')
  assert.equal(formatElapsed(NOW + 60_000, NOW), 'just now')
})

test('status labels describe the user-visible state', () => {
  assert.equal(statusLabel({ group: 'working', tab: tab(), status: status({ status: 'working' }) }), 'Working')
  assert.equal(statusLabel({ group: 'needs', tab: tab(), status: status({ pending_question: true }) }), 'Waiting for you')
  assert.equal(statusLabel({ group: 'needs', tab: tab(), status: status({ status: 'attention' }) }), 'Needs attention')
  assert.equal(statusLabel({ group: 'done', tab: tab(), status: status() }), 'Idle')
})

test('inline reply is only offered where it cannot collide with a turn or a card', () => {
  assert.equal(canReplyInline({ chatTab: true, pendingQuestion: false, working: false }), true)
  assert.equal(canReplyInline({ chatTab: true, pendingQuestion: true, working: false }), false)
  assert.equal(canReplyInline({ chatTab: true, pendingQuestion: false, working: true }), false)
  assert.equal(canReplyInline({ chatTab: false, pendingQuestion: false, working: false }), false)
})

test('the panel keyboard leaves its own controls their native Enter', () => {
  // A container-level keydown must not turn "activate this filter/refresh/
  // toggle/expand/reply" into "open the cursor row".
  const onControl = { fromButton: true, fromTextEntry: false, hasRow: true }
  assert.equal(resolveInboxKey({ key: 'Enter', ...onControl }), null)
  assert.equal(resolveInboxKey({ key: 'Enter', fromButton: false, fromTextEntry: true, hasRow: true }), null)
  assert.equal(resolveInboxKey({ key: 'Enter', fromButton: false, fromTextEntry: false, hasRow: true }), 'open')
  // Nothing to open is not an action.
  assert.equal(resolveInboxKey({ key: 'Enter', fromButton: false, fromTextEntry: false, hasRow: false }), null)
})

test('the panel keyboard moves the cursor only outside text fields', () => {
  assert.equal(resolveInboxKey({ key: 'ArrowDown', fromButton: false, fromTextEntry: false, hasRow: true }), 'move-down')
  assert.equal(resolveInboxKey({ key: 'ArrowUp', fromButton: true, fromTextEntry: false, hasRow: true }), 'move-up')
  // Inside the reply field the arrows belong to the caret.
  for (const key of ['ArrowDown', 'ArrowUp', 'ArrowLeft', 'ArrowRight']) {
    assert.equal(resolveInboxKey({ key, fromButton: false, fromTextEntry: true, hasRow: true }), null, key)
  }
})

test('the panel keyboard expands, collapses and closes from anywhere', () => {
  assert.equal(resolveInboxKey({ key: 'ArrowRight', fromButton: false, fromTextEntry: false, hasRow: true }), 'expand')
  assert.equal(resolveInboxKey({ key: 'ArrowLeft', fromButton: false, fromTextEntry: false, hasRow: true }), 'collapse')
  assert.equal(resolveInboxKey({ key: 'ArrowRight', fromButton: false, fromTextEntry: false, hasRow: false }), null)
  // Escape is the way out even from a text field.
  assert.equal(resolveInboxKey({ key: 'Escape', fromButton: false, fromTextEntry: true, hasRow: true }), 'close')
  assert.equal(resolveInboxKey({ key: 'Escape', fromButton: true, fromTextEntry: false, hasRow: false }), 'close')
  // Unrelated keys pass through.
  assert.equal(resolveInboxKey({ key: 'a', fromButton: false, fromTextEntry: false, hasRow: true }), null)
})

test('keydown targets are classified by their own tag, not by the container', () => {
  const container = { tagName: 'DIV' }
  assert.deepEqual(keyTargetKind(container, container), { fromButton: false, fromTextEntry: false })
  assert.deepEqual(keyTargetKind({ tagName: 'BUTTON' }, container), { fromButton: true, fromTextEntry: false })
  for (const tagName of ['INPUT', 'TEXTAREA', 'SELECT']) {
    assert.deepEqual(keyTargetKind({ tagName }, container), { fromButton: false, fromTextEntry: true }, tagName)
  }
  assert.deepEqual(
    keyTargetKind({ tagName: 'DIV', isContentEditable: true }, container),
    { fromButton: false, fromTextEntry: true },
  )
  // Non-element targets (window, text nodes) are not controls.
  assert.deepEqual(keyTargetKind(null, container), { fromButton: false, fromTextEntry: false })
})

test('reply routes to the managed session when the tab has one', () => {
  assert.equal(
    inlineReplyPath(tab({ id: 'tab 1', managed_session_id: 'session-9' })),
    '/api/workspaces/sessions/session-9/stream/send',
  )
  assert.equal(
    inlineReplyPath(tab({ id: 'tab 1' })),
    '/api/workspaces/tabs/tab%201/stream/send',
  )
})

test('the reply body is the composer send contract, normal delivery, no attachments', () => {
  const body = buildInboxReplyBody({ tab: tab(), text: 'keep going', clientTurnId: 'turn-7' })
  assert.deepEqual(body, {
    client_turn_id: 'turn-7',
    text: 'keep going',
    delivery: 'normal',
    attachments: [],
  })
})

test('a successful reply reports the turn id it used', async t => {
  const calls = []
  t.after(() => {
    delete globalThis.fetch
  })
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init })
    return { ok: true, status: 200, json: async () => ({ ok: true }) }
  }
  const result = await sendInboxReply({ tab: tab(), text: 'go on', clientTurnId: 'turn-1' })
  assert.equal(result.clientTurnId, 'turn-1')
  assert.equal(calls.length, 1)
  assert.equal(calls[0].url, '/api/workspaces/tabs/tab-1/stream/send')
  assert.equal(calls[0].init.method, 'POST')
  assert.equal(calls[0].init.credentials, 'same-origin')
  assert.deepEqual(JSON.parse(calls[0].init.body), {
    client_turn_id: 'turn-1',
    text: 'go on',
    delivery: 'normal',
    attachments: [],
  })
})

test('a rejected reply surfaces the backend reason and keeps the identity', async t => {
  t.after(() => {
    delete globalThis.fetch
  })
  globalThis.fetch = async () => ({
    ok: false,
    status: 409,
    json: async () => ({ detail: 'A turn is already running in this Chat.' }),
  })
  await assert.rejects(
    () => sendInboxReply({ tab: tab(), text: 'go on', clientTurnId: 'turn-2' }),
    /A turn is already running in this Chat\./,
  )
})

test('a non-JSON failure still reports something actionable', async t => {
  t.after(() => {
    delete globalThis.fetch
  })
  globalThis.fetch = async () => ({
    ok: false,
    status: 502,
    json: async () => {
      throw new Error('not json')
    },
  })
  await assert.rejects(
    () => sendInboxReply({ tab: tab(), text: 'go on', clientTurnId: 'turn-3' }),
    /Reply failed \(HTTP 502\)/,
  )
})

test('the surface name is not repeated as a group name', () => {
  // The entry, its panel and its phone sheet all say "Needs you", so the first
  // group cannot say it too: the title names the surface, the groups name the
  // state of the rows inside it.
  const rows = flattenRows(
    buildInbox(
      [tab({ id: 'q', name: 'asked' }), tab({ id: 'w', name: 'busy' })],
      [status({ tab_id: 'q', status: 'attention', pending_question: 'Which file?' }), status({ tab_id: 'w' })],
      { nowMs: NOW },
    ),
  )
  const groups = buildInbox(
    [tab({ id: 'q', name: 'asked' }), tab({ id: 'w', name: 'busy' })],
    [status({ tab_id: 'q', status: 'attention', pending_question: 'Which file?' }), status({ tab_id: 'w' })],
    { nowMs: NOW },
  )
  const titles = groups.map(group => [group.key, group.title])
  assert.deepEqual(titles, [
    ['needs', 'Waiting on you'],
    ['working', 'Working'],
    ['done', 'Recently done'],
  ])
  assert.equal(rows.length, groups.reduce((sum, group) => sum + group.rows.length, 0))
})
