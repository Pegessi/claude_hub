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

test('free-text questions without options remain visible and require an answer', () => {
  const questions = parseStructuredQuestions([
    { id: 'context', prompt: 'What should the plan cover?', options: [] },
    { id: 'scope', prompt: 'Describe the scope' },
  ])
  assert.equal(questions.length, 2)
  assert.equal(isQuestionAnswerComplete(questions, {}), false)
  assert.equal(isQuestionAnswerComplete(questions, { context: ['   '], scope: ['UI'] }), false)
  assert.equal(isQuestionAnswerComplete(questions, { context: ['Settings'], scope: ['UI'] }), true)
})

test('option descriptions survive normalization for the decision UI', () => {
  const [question] = parseStructuredQuestions([
    { id: 'scope', prompt: 'Choose scope', options: [
      { id: 'small', label: 'Small', description: 'Only the sidebar' },
    ] },
  ])
  assert.equal(question.options[0].description, 'Only the sidebar')
})

test('secret free-text questions remain renderable without leaking the flag', () => {
  const [question] = parseStructuredQuestions([
    { id: 'token', prompt: 'Token?', options: null, is_secret: true },
  ])
  assert.equal(question.options.length, 0)
  assert.equal(question.isSecret, true)
})

test('a malformed empty question card cannot be submitted', () => {
  assert.equal(isQuestionAnswerComplete([], {}), false)
})
