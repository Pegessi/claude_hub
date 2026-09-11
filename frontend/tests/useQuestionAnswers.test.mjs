import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import ts from 'typescript'

// ── Load useQuestionAnswers.ts ──────────────────────────────────────────
// The composable imports `ref` from 'vue' and `isQuestionAnswerComplete`
// from '@/utils/chatQuestionResponse'. For the unit test we don't need Vue's
// reactivity — a plain mutable box is enough — so we shim the vue import and
// inline the self-contained chatQuestionResponse module before transpiling.
const transpileOptions = {
  compilerOptions: {
    module: ts.ModuleKind.ES2022,
    target: ts.ScriptTarget.ES2020,
  },
}

const questionSource = await readFile(
  new URL('../src/utils/chatQuestionResponse.ts', import.meta.url),
  'utf8',
)
const questionJs = ts.transpileModule(questionSource, transpileOptions).outputText

const composableSource = await readFile(
  new URL('../src/composables/useQuestionAnswers.ts', import.meta.url),
  'utf8',
)
const shimmedSource = composableSource
  .replace(
    `import { ref, type Ref } from 'vue'`,
    `const ref = (initial) => ({ value: initial })`,
  )
  // Inlined above (questionJs); drop the runtime import so the bundle resolves.
  .replace(/import \{[\s\S]*?\} from '@\/utils\/chatQuestionResponse'\n?/, '')
const composableJs = ts.transpileModule(shimmedSource, transpileOptions).outputText

const bundled = `${questionJs}\n${composableJs}`
const mod = await import(
  `data:text/javascript;base64,${Buffer.from(bundled).toString('base64')}`
)
const { useQuestionAnswers, approvalStateSignature, formatAskQuestionResponse } = mod

function makeApproval(overrides = {}) {
  return {
    key: 'approval-call-1',
    callId: 'call-1',
    kind: 'ask_question',
    title: 'Pick one',
    questions: [
      {
        id: 'q1',
        prompt: 'Pick one',
        allowMultiple: false,
        options: [
          { id: 'a', label: 'A' },
          { id: 'b', label: 'B' },
        ],
      },
    ],
    resolved: false,
    ...overrides,
  }
}

function makeMultiApproval() {
  return makeApproval({
    questions: [
      {
        id: 'q1',
        prompt: 'Pick any',
        allowMultiple: true,
        options: [
          { id: 'a', label: 'A' },
          { id: 'b', label: 'B' },
          { id: 'c', label: 'C' },
        ],
      },
    ],
  })
}

// ── Single-select ────────────────────────────────────────────────────────

test('single-select toggles an option on and reflects it as selected', () => {
  const qa = useQuestionAnswers()
  const approval = makeApproval()
  assert.equal(qa.isQuestionOptionSelected(approval.key, 'q1', 'a'), false)
  qa.toggleQuestionOption(approval.key, 'q1', 'a', false)
  assert.equal(qa.isQuestionOptionSelected(approval.key, 'q1', 'a'), true)
  assert.deepEqual(qa.answersFor(approval.key), { q1: ['a'] })
})

test('single-select replaces the prior selection instead of accumulating', () => {
  const qa = useQuestionAnswers()
  const approval = makeApproval()
  qa.toggleQuestionOption(approval.key, 'q1', 'a', false)
  qa.toggleQuestionOption(approval.key, 'q1', 'b', false)
  assert.equal(qa.isQuestionOptionSelected(approval.key, 'q1', 'a'), false)
  assert.equal(qa.isQuestionOptionSelected(approval.key, 'q1', 'b'), true)
  assert.deepEqual(qa.answersFor(approval.key), { q1: ['b'] })
})

// ── Multi-select ─────────────────────────────────────────────────────────

test('multi-select toggles several options on and off independently', () => {
  const qa = useQuestionAnswers()
  const approval = makeMultiApproval()
  qa.toggleQuestionOption(approval.key, 'q1', 'a', true)
  qa.toggleQuestionOption(approval.key, 'q1', 'c', true)
  assert.equal(qa.isQuestionOptionSelected(approval.key, 'q1', 'a'), true)
  assert.equal(qa.isQuestionOptionSelected(approval.key, 'q1', 'b'), false)
  assert.equal(qa.isQuestionOptionSelected(approval.key, 'q1', 'c'), true)
  assert.deepEqual(qa.answersFor(approval.key), { q1: ['a', 'c'] })

  // Toggling a selected option off removes just that option.
  qa.toggleQuestionOption(approval.key, 'q1', 'a', true)
  assert.equal(qa.isQuestionOptionSelected(approval.key, 'q1', 'a'), false)
  assert.equal(qa.isQuestionOptionSelected(approval.key, 'q1', 'c'), true)
  assert.deepEqual(qa.answersFor(approval.key), { q1: ['c'] })
})

test('selections for different approvals never bleed into each other', () => {
  const qa = useQuestionAnswers()
  const a1 = makeApproval({ key: 'approval-1' })
  const a2 = makeApproval({ key: 'approval-2' })
  qa.toggleQuestionOption(a1.key, 'q1', 'a', false)
  assert.equal(qa.isQuestionOptionSelected(a2.key, 'q1', 'a'), false)
  assert.deepEqual(qa.answersFor(a2.key), {})
})

// ── Submit gating ─────────────────────────────────────────────────────────

test('canSubmitQuestion is false until every question has a selection', () => {
  const qa = useQuestionAnswers()
  const approval = makeApproval({
    questions: [
      {
        id: 'q1',
        prompt: 'One',
        allowMultiple: false,
        options: [{ id: 'a', label: 'A' }],
      },
      {
        id: 'q2',
        prompt: 'Two',
        allowMultiple: false,
        options: [{ id: 'b', label: 'B' }],
      },
    ],
  })
  assert.equal(qa.canSubmitQuestion(approval), false)
  qa.toggleQuestionOption(approval.key, 'q1', 'a', false)
  assert.equal(qa.canSubmitQuestion(approval), false, 'one unanswered question still blocks submit')
  qa.toggleQuestionOption(approval.key, 'q2', 'b', false)
  assert.equal(qa.canSubmitQuestion(approval), true)
})

// ── Resolved / disabled states ───────────────────────────────────────────

test('isApprovalResolved is false for a fresh unanswered approval', () => {
  const qa = useQuestionAnswers()
  const approval = makeApproval()
  assert.equal(qa.isApprovalResolved(approval), false)
})

test('markResolved marks the approval resolved (disables further selection)', () => {
  const qa = useQuestionAnswers()
  const approval = makeApproval()
  qa.markResolved(approval.key)
  assert.equal(qa.isApprovalResolved(approval), true)
})

test('a backend-marked resolved approval is resolved without a local mark', () => {
  const qa = useQuestionAnswers()
  const approval = makeApproval({ resolved: true })
  assert.equal(qa.isApprovalResolved(approval), true)
})

test('reset clears selections and resolved keys (tab switch)', () => {
  const qa = useQuestionAnswers()
  const approval = makeApproval()
  qa.toggleQuestionOption(approval.key, 'q1', 'a', false)
  qa.markResolved(approval.key)
  qa.reset()
  assert.equal(qa.isQuestionOptionSelected(approval.key, 'q1', 'a'), false)
  assert.equal(qa.isApprovalResolved(approval), false)
  assert.deepEqual(qa.answersFor(approval.key), {})
})

// ── v-memo signature (the regression guard) ──────────────────────────────
// StructuredPane memoizes each turn with v-memo="[renderRevision,
// erroredAttachments.size, turnApprovalSignature(turn)]". The signature MUST
// change when the selection or resolved state changes, or the memoized turn
// is never re-rendered and the chip shows no selected state / the submit
// button stays disabled — exactly the reported bug.

test('approvalStateSignature changes when an option is selected', () => {
  const approval = makeApproval()
  const before = approvalStateSignature(approval, {}, new Set(), {})
  const after = approvalStateSignature(
    approval,
    { [approval.key]: { q1: ['a'] } },
    new Set(),
    {},
  )
  assert.notEqual(before, after, 'signature must change on selection')
})

test('approvalStateSignature changes when the approval is resolved', () => {
  const approval = makeApproval()
  const answers = { [approval.key]: { q1: ['a'] } }
  const before = approvalStateSignature(approval, answers, new Set(), {})
  const after = approvalStateSignature(approval, answers, new Set([approval.key]), {})
  assert.notEqual(before, after, 'signature must change on resolve')
})

test('approvalStateSignature is stable when nothing changed (keeps memoization)', () => {
  const approval = makeApproval()
  const answers = { [approval.key]: { q1: ['a'] } }
  const resolved = new Set([approval.key])
  assert.equal(
    approvalStateSignature(approval, answers, resolved, {}),
    approvalStateSignature(approval, { ...answers }, new Set(resolved), {}),
    'identical state must produce an identical signature so v-memo can skip re-render',
  )
})

test('approvalStateSignature distinguishes single-select replacement', () => {
  const approval = makeApproval()
  const a = approvalStateSignature(approval, { [approval.key]: { q1: ['a'] } }, new Set(), {})
  const b = approvalStateSignature(approval, { [approval.key]: { q1: ['b'] } }, new Set(), {})
  assert.notEqual(a, b)
})


// ── Free-text answers ───────────────────────────────────────────────────
//
// The listed options are the agent's guess at the answer, not the whole space
// of them. A typed answer has to behave like any other selection or it would
// be silently dropped: it must satisfy the completion check, survive a
// multi-select, clear when blank, and travel in the same payload.

test('a typed answer satisfies the completion check', () => {
  const { customAnswer, setCustomAnswer, canSubmitQuestion } = useQuestionAnswers()
  const approval = makeApproval()
  assert.equal(canSubmitQuestion(approval), false, 'nothing chosen yet')

  setCustomAnswer(approval.key, approval.questions[0], '第三个方案')
  assert.equal(customAnswer(approval.key, approval.questions[0]), '第三个方案')
  assert.equal(canSubmitQuestion(approval), true, 'the box being filled is an answer')
})

test('typing replaces a ticked option on a single-select question', () => {
  const { customAnswer, setCustomAnswer, toggleQuestionOption, answersFor } = useQuestionAnswers()
  const approval = makeApproval()
  const question = approval.questions[0]

  toggleQuestionOption(approval.key, question.id, 'a', false)
  setCustomAnswer(approval.key, question, '都不是')
  assert.deepEqual(answersFor(approval.key)[question.id], ['都不是'])
  assert.equal(customAnswer(approval.key, question), '都不是')

  // And picking an option again clears the typed answer: one answer, not two.
  toggleQuestionOption(approval.key, question.id, 'a', false)
  assert.deepEqual(answersFor(approval.key)[question.id], ['a'])
  assert.equal(customAnswer(approval.key, question), '')
})

test('typing joins the ticked options on a multi-select question', () => {
  const { customAnswer, setCustomAnswer, toggleQuestionOption, answersFor } = useQuestionAnswers()
  const approval = makeMultiApproval()
  const question = approval.questions[0]

  toggleQuestionOption(approval.key, question.id, 'a', true)
  setCustomAnswer(approval.key, question, '还有别的')
  assert.deepEqual(answersFor(approval.key)[question.id], ['a', '还有别的'])
  assert.equal(customAnswer(approval.key, question), '还有别的')
})

test('whitespace only is not an answer', () => {
  const { customAnswer, setCustomAnswer, canSubmitQuestion, answersFor } = useQuestionAnswers()
  const approval = makeApproval()
  const question = approval.questions[0]

  setCustomAnswer(approval.key, question, '   ')
  assert.equal(answersFor(approval.key)[question.id], undefined, 'whitespace is not submitted')
  assert.equal(canSubmitQuestion(approval), false, 'and the question stays unanswered')
  assert.equal(customAnswer(approval.key, question), '   ', 'but the box keeps what was typed')
})

test('a typed answer survives unticking a multi-select option', () => {
  const { customAnswer, setCustomAnswer, toggleQuestionOption, answersFor } = useQuestionAnswers()
  const approval = makeMultiApproval()
  const question = approval.questions[0]

  toggleQuestionOption(approval.key, question.id, 'a', true)
  setCustomAnswer(approval.key, question, '另加一组')
  toggleQuestionOption(approval.key, question.id, 'a', true)
  assert.deepEqual(answersFor(approval.key)[question.id], ['另加一组'])
  assert.equal(customAnswer(approval.key, question), '另加一组')
})

test('typing an option\'s own text is still a typed answer, not a tick', () => {
  // The option id IS its label in this codebase, so inferring "is this typed?"
  // from the id set read the text back as a tick and emptied the box.
  const { customAnswer, setCustomAnswer, isQuestionOptionSelected, answersFor } = useQuestionAnswers()
  const approval = makeApproval()
  const question = approval.questions[0]

  setCustomAnswer(approval.key, question, 'A')
  assert.equal(customAnswer(approval.key, question), 'A', 'the box keeps the text')
  assert.equal(isQuestionOptionSelected(approval.key, question.id, 'a'), false, 'no option was ticked')
  assert.deepEqual(answersFor(approval.key)[question.id], ['A'])
})

test('a typed answer travels in the submitted payload', () => {
  const { setCustomAnswer, answersFor } = useQuestionAnswers()
  const approval = makeApproval()
  setCustomAnswer(approval.key, approval.questions[0], 'C')
  const text = formatAskQuestionResponse(answersFor(approval.key))
  assert.deepEqual(JSON.parse(text), {
    type: 'ask_question_response',
    answers: [{ questionId: 'q1', selected: ['C'] }],
  })
})


test('the typed answer is part of the memo signature', () => {
  // Without this the keystroke updates state but v-memo skips the turn, so the
  // submit button never re-enables. The signature is the only thing standing
  // between a typed answer and a dead button.
  const approval = makeApproval()
  const answers = { [approval.key]: { q1: [] } }
  const blank = approvalStateSignature(approval, answers, new Set(), {
    [approval.key]: { q1: '' },
  })
  const typed = approvalStateSignature(approval, answers, new Set(), {
    [approval.key]: { q1: '第三个方案' },
  })
  assert.notEqual(blank, typed, 'a keystroke must change the signature')
  assert.equal(
    approvalStateSignature(approval, answers, new Set(), { [approval.key]: { q1: 'x' } }),
    approvalStateSignature(approval, answers, new Set(), { [approval.key]: { q1: 'x' } }),
    'and identical text must keep it stable so memoization survives',
  )
})

test('reset clears typed answers along with selections', () => {
  // A tab switch must not carry a half-typed answer into the next card.
  const { customAnswer, setCustomAnswer, reset } = useQuestionAnswers()
  const approval = makeApproval()
  setCustomAnswer(approval.key, approval.questions[0], '写了半截')
  reset()
  assert.equal(customAnswer(approval.key, approval.questions[0]), '')
})
