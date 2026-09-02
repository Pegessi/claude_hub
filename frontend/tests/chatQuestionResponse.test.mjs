import test from 'node:test'
import assert from 'node:assert/strict'

import {
  formatAskQuestionResponse,
  isQuestionAnswerComplete,
  parseStructuredQuestions,
} from '../src/utils/chatQuestionResponse.ts'

test('parseStructuredQuestions normalizes ask payloads', () => {
  const questions = parseStructuredQuestions([
    {
      id: 'scope',
      prompt: 'Pick one',
      options: [{ id: 'a', label: 'A' }, { id: 'b', label: 'B' }],
    },
  ])
  assert.equal(questions.length, 1)
  assert.equal(questions[0].id, 'scope')
  assert.deepEqual(questions[0].options.map(o => o.id), ['a', 'b'])
})

test('isQuestionAnswerComplete requires every question to have a selection', () => {
  const questions = parseStructuredQuestions([
    { id: 'q1', prompt: 'One', options: [{ id: 'a', label: 'A' }] },
    { id: 'q2', prompt: 'Two', options: [{ id: 'b', label: 'B' }] },
  ])
  assert.equal(isQuestionAnswerComplete(questions, { q1: ['a'] }), false)
  assert.equal(isQuestionAnswerComplete(questions, { q1: ['a'], q2: ['b'] }), true)
})

test('formatAskQuestionResponse emits stable JSON', () => {
  const payload = formatAskQuestionResponse({ q1: ['local_only'] })
  assert.match(payload, /"type":"ask_question_response"/)
  assert.match(payload, /"questionId":"q1"/)
})
