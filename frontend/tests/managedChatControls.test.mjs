import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { ref } from 'vue'
import ts from 'typescript'

const source = await readFile(
  new URL('../src/components/StructuredPane.vue', import.meta.url),
  'utf8',
)

function extractFunction(name) {
  const asyncStart = source.indexOf(`async function ${name}(`)
  const syncStart = source.indexOf(`function ${name}(`)
  const start = asyncStart >= 0 ? asyncStart : syncStart
  assert.ok(start >= 0, `${name} must exist`)
  const brace = source.indexOf('{', start)
  let depth = 0
  for (let index = brace; index < source.length; index += 1) {
    if (source[index] === '{') depth += 1
    else if (source[index] === '}') {
      depth -= 1
      if (depth === 0) return source.slice(start, index + 1)
    }
  }
  throw new Error(`could not extract ${name}`)
}

function compile(name, dependencyNames) {
  const { outputText } = ts.transpileModule(extractFunction(name), {
    compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2020 },
  })
  return new Function(
    'deps',
    `const { ${dependencyNames.join(', ')} } = deps;\n${outputText}\nreturn ${name};`,
  )
}

const makeSubmitQuestionResponse = compile('submitQuestionResponse', [
  'canSubmitQuestion',
  'composerError',
  'isManagedChat',
  'turnInFlight',
  'submit',
  'formatAskQuestionResponse',
  'answersFor',
  'markResolved',
])

const makeHandleComposerEnter = compile('handleComposerEnter', [
  'goalInputBlocked',
  'composerError',
  'goalComposerReason',
  'resolveComposerEnterAction',
  'isComposing',
  'turnInFlight',
  'goalSteerOnly',
  'draftMessage',
  'attachments',
  'isMobileViewport',
  'isManagedChat',
  'submit',
  'enqueueDraft',
])

const makeCancelActiveTurn = compile('cancelActiveTurn', [
  'isCancelling',
  'isSending',
  'stopArmed',
  'stopTargetTurnId',
  'composerError',
  'isManagedChat',
  'streamBasePath',
  'optimisticCancelledTurnId',
  'nudge',
  'terminalStore',
])

test('managed question submission carries exact card identity as a normal delivery', async () => {
  const composerError = ref(null)
  const calls = []
  const resolved = []
  const submitQuestionResponse = makeSubmitQuestionResponse({
    canSubmitQuestion: () => true,
    composerError,
    isManagedChat: ref(true),
    turnInFlight: ref(true),
    submit: async (...args) => {
      calls.push(args)
      return true
    },
    formatAskQuestionResponse: answers => JSON.stringify(answers),
    answersFor: key => ({ [`answer-for-${key}`]: ['red'] }),
    markResolved: key => resolved.push(key),
  })

  await submitQuestionResponse(
    { turnId: 'turn-current' },
    { key: 'card-current', callId: 'call-current' },
  )

  assert.equal(composerError.value, null)
  assert.equal(calls.length, 1)
  assert.equal(calls[0][0], 'normal')
  assert.equal(calls[0][2], 'user')
  assert.deepEqual(calls[0][3], { turnId: 'turn-current', callId: 'call-current' })
  assert.deepEqual(resolved, ['card-current'])
})

test('question submission fails closed when provider identity is incomplete', async () => {
  const composerError = ref(null)
  let submitted = false
  const submitQuestionResponse = makeSubmitQuestionResponse({
    canSubmitQuestion: () => true,
    composerError,
    isManagedChat: ref(true),
    turnInFlight: ref(false),
    submit: async () => {
      submitted = true
      return true
    },
    formatAskQuestionResponse: () => '{}',
    answersFor: () => ({}),
    markResolved() {},
  })

  await submitQuestionResponse({ turnId: 'turn-current' }, { key: 'card', callId: '' })

  assert.equal(submitted, false)
  assert.match(composerError.value, /missing its current request identity/)
})

test('managed busy Enter and Ctrl+Enter durably submit instead of using the local queue', () => {
  const submissions = []
  let queued = 0
  let prevented = 0
  const deps = {
    goalInputBlocked: ref(false),
    composerError: ref(null),
    goalComposerReason: ref(null),
    resolveComposerEnterAction: ({ ctrlKey, metaKey }) => ctrlKey || metaKey ? 'steer' : 'queue',
    isComposing: ref(false),
    turnInFlight: ref(true),
    goalSteerOnly: ref(false),
    draftMessage: ref('durable follow-up'),
    attachments: ref([]),
    isMobileViewport: ref(false),
    isManagedChat: ref(true),
    submit: delivery => {
      submissions.push(delivery)
      return Promise.resolve(true)
    },
    enqueueDraft: () => { queued += 1 },
  }
  const handleComposerEnter = makeHandleComposerEnter(deps)
  const event = ctrlKey => ({
    isComposing: false,
    shiftKey: false,
    metaKey: false,
    ctrlKey,
    altKey: false,
    preventDefault: () => { prevented += 1 },
  })

  handleComposerEnter(event(false))
  handleComposerEnter(event(true))

  assert.deepEqual(submissions, ['normal', 'normal'])
  assert.equal(queued, 0)
  assert.equal(prevented, 2)

  deps.isManagedChat.value = false
  handleComposerEnter(event(false))
  handleComposerEnter(event(true))
  assert.equal(queued, 1)
  assert.deepEqual(submissions, ['normal', 'normal', 'steer'])
})

function cancelDeps() {
  let nudges = 0
  let statusRefreshes = 0
  const deps = {
    isCancelling: ref(false),
    isSending: ref(false),
    stopArmed: ref(true),
    stopTargetTurnId: ref('turn-current'),
    composerError: ref(null),
    isManagedChat: ref(true),
    streamBasePath: ref('/api/workspaces/sessions/managed-1/stream'),
    optimisticCancelledTurnId: ref(null),
    nudge() { nudges += 1 },
    terminalStore: {
      async fetchAgentStatuses() { statusRefreshes += 1 },
    },
  }
  return {
    deps,
    getNudges: () => nudges,
    getStatusRefreshes: () => statusRefreshes,
  }
}

test('stale managed Stop remains a conflict and cannot cancel a newer turn', async t => {
  const state = cancelDeps()
  t.mock.method(globalThis, 'fetch', async () => ({
    ok: false,
    status: 409,
    json: async () => ({ detail: 'A newer managed Chat turn is active' }),
  }))
  const cancelActiveTurn = makeCancelActiveTurn(state.deps)

  await cancelActiveTurn()

  assert.match(state.deps.composerError.value, /newer managed Chat turn/)
  assert.equal(state.deps.optimisticCancelledTurnId.value, null)
  assert.equal(state.getNudges(), 0)
  assert.equal(state.getStatusRefreshes(), 0)
})

test('already-terminal managed Stop is idempotent and releases local UI optimism', async t => {
  const state = cancelDeps()
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    assert.equal(url, '/api/workspaces/sessions/managed-1/stream/cancel')
    assert.deepEqual(JSON.parse(options.body), { expected_turn_id: 'turn-current' })
    return {
      ok: true,
      status: 200,
      json: async () => ({ ok: true, cancelled: false, outcome: 'already_terminal' }),
    }
  })
  const cancelActiveTurn = makeCancelActiveTurn(state.deps)

  await cancelActiveTurn()
  await new Promise(resolve => setTimeout(resolve, 0))

  assert.equal(state.deps.composerError.value, null)
  assert.equal(state.deps.optimisticCancelledTurnId.value, 'turn-current')
  assert.equal(state.getNudges(), 1)
  assert.equal(state.getStatusRefreshes(), 1)
})
