import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { loadChatUtility } from './chatPresentationTestHarness.mjs'

const approvalCard = readFileSync(
  new URL('../src/features/chat/components/ChatApprovalCard.vue', import.meta.url),
  'utf8',
)
const receipt = readFileSync(
  new URL('../src/features/chat/components/useQuestionReceipt.ts', import.meta.url),
  'utf8',
)
const composer = readFileSync(
  new URL('../src/features/chat/controllers/useChatComposer.ts', import.meta.url),
  'utf8',
)
const {
  describeQuestionAnswers,
  isQuestionAnswered,
  latestPendingApprovalKey,
  parseStructuredQuestions,
  splitRecommendedLabel,
} = await loadChatUtility('chatQuestionResponse')

const question = {
  id: 'q1',
  prompt: 'Which scope?',
  allowMultiple: false,
  options: [
    { id: 'ui', label: 'Frontend only' },
    { id: 'api', label: 'API too' },
  ],
}

test('a provider "(Recommended)" label becomes a marker, not part of the choice', () => {
  assert.deepEqual(splitRecommendedLabel('Keep it small (Recommended)'), {
    label: 'Keep it small',
    recommended: true,
  })
  assert.deepEqual(splitRecommendedLabel('Keep it small（recommended）'), {
    label: 'Keep it small',
    recommended: true,
  })
  assert.deepEqual(splitRecommendedLabel('Keep it small'), { label: 'Keep it small', recommended: false })
  // A label that is only the marker stays visible instead of becoming empty.
  assert.deepEqual(splitRecommendedLabel('(Recommended)'), { label: '(Recommended)', recommended: false })
})

test('parsed options keep their tradeoff line and recommendation flag', () => {
  const [parsed] = parseStructuredQuestions([
    {
      id: 'scope',
      prompt: 'Which scope?',
      options: [
        { id: 'ui', label: 'Frontend only (Recommended)', description: 'Fastest; API stays as is' },
        { id: 'api', label: 'API too' },
      ],
    },
  ])
  assert.deepEqual(parsed.options[0], {
    id: 'ui',
    label: 'Frontend only',
    description: 'Fastest; API stays as is',
    recommended: true,
  })
  assert.deepEqual(parsed.options[1], { id: 'api', label: 'API too' })
  // The id never changes: it is what the answer payload carries.
  assert.equal(parsed.options[0].id, 'ui')
})

test('answers are readable on the resolved line', () => {
  assert.equal(describeQuestionAnswers([question], { q1: ['api'] }), 'API too')
  assert.equal(
    describeQuestionAnswers(
      [question, { ...question, id: 'q2', prompt: 'Timing?' }],
      { q1: ['api'], q2: ['now'] },
    ),
    'API too · now',
  )
  assert.equal(
    describeQuestionAnswers([{ ...question, allowMultiple: true }], { q1: ['ui', 'api'] }),
    'Frontend only, API too',
  )
  // A typed answer has no option to map to, so it is shown verbatim.
  assert.equal(describeQuestionAnswers([question], { q1: ['both halves'] }), 'both halves')
  assert.equal(describeQuestionAnswers([question], {}), '')
})

test('one question is answered without answering the others', () => {
  assert.equal(isQuestionAnswered(question, {}), false)
  assert.equal(isQuestionAnswered(question, { q1: ['  '] }), false)
  assert.equal(isQuestionAnswered(question, { q1: ['ui'] }), true)
})

test('only the last unresolved approval owns the shortcuts', () => {
  assert.equal(latestPendingApprovalKey([]), null)
  assert.equal(
    latestPendingApprovalKey([
      { key: 'approval-a', resolved: true },
      { key: 'approval-b', resolved: false },
      { key: 'approval-c', resolved: true },
    ]),
    'approval-b',
  )
  assert.equal(
    latestPendingApprovalKey([{ key: 'approval-a', resolved: false }, { key: 'approval-b', resolved: false }]),
    'approval-b',
  )
})

test('the card shows the question, its tradeoffs, the marker and the progress', () => {
  assert.match(approvalCard, /class="approval-option-text">\{\{ option\.label \}\}/)
  assert.match(approvalCard, /v-if="option\.description"[\s\S]{0,120}\{\{ option\.description \}\}/)
  assert.match(approvalCard, /v-if="option\.recommended"[\s\S]{0,160}Recommended/)
  assert.match(approvalCard, /`Question \$\{[^}]*\} of \$\{[^}]*\}`/)
  // The question itself is the card title, not the provider's short header.
  assert.match(approvalCard, /The question itself is the title/)
  assert.match(approvalCard, /`You chose: \$\{chosenText\.value\}`/)
  assert.match(approvalCard, /Press 1–9 to choose/)
})

test('the card answers with number keys only while it is the latest open decision', () => {
  assert.match(approvalCard, /latestPendingApprovalKey\(approvals\) === props\.part\.approval\.key/)
  assert.match(approvalCard, /if \(!\/\^\[1-9\]\$\/\.test\(event\.key\)\) return/)
  assert.match(approvalCard, /isEditableTarget\(event\.target\)/)
  assert.match(approvalCard, /addEventListener\('keydown', handleDocumentKeydown\)/)
  assert.match(approvalCard, /removeEventListener\('keydown', handleDocumentKeydown\)/)
  assert.match(approvalCard, /options\[Number\(event\.key\) - 1\]/)
  // Tabbed into an older card still answers it; a dialog keeps its own keys.
  assert.match(approvalCard, /if \(!isLatestPending\.value && !hasFocusInside\(\)\) return/)
  assert.match(approvalCard, /root\.contains\(active\)/)
  assert.match(approvalCard, /\[role="dialog"\]\[aria-modal="true"\]/)
})

test('an answered card collapses to one line and drops the form', () => {
  assert.match(approvalCard, /v-if="answered"[\s\S]{0,400}approval-card--resolved/)
  assert.match(approvalCard, /v-else[\s\S]{0,200}class="approval-card"/)
  assert.match(approvalCard, /receipt\.state\.value === 'resolved' \? 'Answered'/)
})

test('approval product strings are English', () => {
  for (const legacy of ['需要你的选择', '已提交', '提交选择', '输入你的回答', '其他（']) {
    assert.equal(approvalCard.includes(legacy), false, `approval card still uses ${legacy}`)
    assert.equal(receipt.includes(legacy), false, `receipt labels still use ${legacy}`)
  }
  for (const legacy of ['请选择所有问题的选项后再提交']) {
    assert.equal(composer.includes(legacy), false, `composer error still uses ${legacy}`)
  }
  assert.match(receipt, /claimed: 'Answer received — delivering'/)
  assert.match(receipt, /resolved: 'Answered'/)
})
