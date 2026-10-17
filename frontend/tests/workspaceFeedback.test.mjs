import assert from 'node:assert/strict'
import test from 'node:test'
import { loadWorkspaceProjection } from './workspaceProjectionTestHarness.mjs'

const {
  matchingFeedbackLessons, lessonTitle, lessonDescription, lessonTags,
  feedbackSummaryTone, feedbackSummaryTitle, feedbackSummaryDescription, feedbackLessonMatchSummary,
} = await loadWorkspaceProjection('workspaceFeedback')

function lesson(id, overrides = {}) {
  return { id, summary: '', ...overrides }
}

const query = { title: 'Preserve Alpha beta', prompt: 'ALPHA 字段验证' }

test('lesson matching counts distinct case-insensitive tokens and preserves tied order', () => {
  const weak = lesson('weak', { summary: 'alpha alpha alpha' })
  const first = lesson('first', { do: 'alpha beta' })
  const second = lesson('second', { applies_when: ['ALPHA'], avoid: 'beta' })
  const cjk = lesson('cjk', { summary: '字段验证' })
  const unrelated = lesson('unrelated', { title: 'alpha beta', summary: 'gamma' })
  const lessons = Object.freeze([weak, first, second, cjk, unrelated])
  assert.deepEqual(matchingFeedbackLessons(query, lessons), [cjk, first, second, weak])
  assert.equal(matchingFeedbackLessons(query, lessons)[0], cjk)
  assert.deepEqual(lessons, [weak, first, second, cjk, unrelated])
})

test('lesson matching uses ids and tags, ignores short tokens, and caps at six', () => {
  assert.deepEqual(matchingFeedbackLessons({ title: 'x 单', prompt: '' }, [lesson('x', { summary: '单' })]), [])
  const byId = lesson('alpha')
  const byTag = lesson('tagged', { tags: ['beta'] })
  assert.deepEqual(matchingFeedbackLessons(query, [byId, byTag]), [byId, byTag])
  const many = Array.from({ length: 8 }, (_, index) => lesson(`item-${index}`, { summary: 'alpha' }))
  assert.deepEqual(matchingFeedbackLessons(query, many), many.slice(0, 6))
  assert.deepEqual(matchingFeedbackLessons(null, many), [])
  assert.deepEqual(matchingFeedbackLessons(query, []), [])
})

test('lesson labels retain title, first sentence, description, and tag fallback rules', () => {
  assert.equal(lessonTitle(lesson('fallback', { title: '  Heading  ', summary: 'First. Second' })), 'Heading')
  assert.equal(lessonTitle(lesson('fallback', { title: ' ', summary: ' 首句。后句' })), '首句')
  assert.equal(lessonTitle(lesson('fallback')), 'fallback')
  assert.equal(lessonDescription(lesson('fallback', { do: 'Do this', avoid: 'Avoid that' })), 'Do this')
  assert.equal(lessonDescription(lesson('fallback', { avoid: 'Avoid that' })), 'Avoid that')
  assert.equal(lessonDescription(lesson('fallback')), 'fallback')
  const tags = ['a', 'b', 'c', 'd', 'e']
  assert.deepEqual(lessonTags(lesson('tags', { tags })), tags.slice(0, 4))
  assert.notEqual(lessonTags(lesson('tags', { tags })), tags)
  assert.deepEqual(lessonTags(lesson('tags')), [])
})

test('summary task id takes priority over skip reasons and cache status', () => {
  const run = { task_id: 'summary-task', skipped_reason: 'no_new_task_records', cache_hit: true }
  assert.equal(feedbackSummaryTone(run), 'queued')
  assert.equal(feedbackSummaryTitle(run), 'Internal AI summary queued')
  assert.equal(feedbackSummaryDescription(run), 'A hidden Feedback Reaper task was started. It will update lessons and write audit evidence when it finishes.')
})

test('summary empty, skipped, cached, and recorded variants retain their wording', () => {
  const empty = { skipped_reason: 'no_new_task_records' }
  assert.equal(feedbackSummaryTone(empty), 'skipped')
  assert.equal(feedbackSummaryTitle(empty), 'No new task records')
  assert.equal(feedbackSummaryDescription(empty), 'No completed task records are available or changed, so no internal AI task was started. Force AI run can reprocess cached records once this workspace has records.')
  assert.equal(feedbackSummaryTitle({ skipped_reason: 'blocked' }), 'Summary skipped')
  assert.equal(feedbackSummaryDescription({ skipped_reason: 'blocked' }), 'No internal AI task was started: blocked.')
  assert.equal(feedbackSummaryTone({ cache_hit: true }), 'skipped')
  assert.equal(feedbackSummaryTitle({ cache_hit: true }), 'Summary recorded')
  assert.equal(feedbackSummaryTone({}), 'done')
  assert.equal(feedbackSummaryDescription({}), 'The feedback summary run was recorded.')
})

test('match summary counts matching tasks, not number of lessons', () => {
  const lessons = [lesson('alpha'), lesson('beta')]
  assert.equal(feedbackLessonMatchSummary([query], []), 'No active lessons indexed')
  assert.equal(feedbackLessonMatchSummary([{ title: 'other', prompt: '' }], lessons), 'No current task matches')
  assert.equal(feedbackLessonMatchSummary([query, query], lessons), '2 current tasks match active lessons')
})
