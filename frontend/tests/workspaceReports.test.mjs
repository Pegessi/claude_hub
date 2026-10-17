import assert from 'node:assert/strict'
import test from 'node:test'
import { loadWorkspaceProjection } from './workspaceProjectionTestHarness.mjs'

const {
  goalPacketSections, goalPacketStatusLabel, goalPacketGateLabel, acceptanceChecksFor,
  reportSummaryLabel, reportMessageForLang, reportMessagePreview, hasBilingualReport,
  profileResultsFor, reviewProfileLabel, taskReviewProfiles, profileResultSummary,
  primaryExpandedReportId, isMarkdownArtifact, markdownArtifactRefs, artifactPreviewKey,
  markdownDocumentPreviewKey, markdownDocumentSourceLabel, taskMarkdownDocuments,
} = await loadWorkspaceProjection('reports')

function report(id, overrides = {}) {
  return { id, state: 'working', message: 'legacy text', ...overrides }
}

test('Goal Packet sections keep order and tolerate absent or non-array fields', () => {
  const criteria = ['deliver tests']
  const sections = goalPacketSections({ acceptance_criteria: criteria, validation_plan: null, assumptions: 'invalid' })
  assert.deepEqual(sections.map(({ key, label }) => [key, label]), [
    ['acceptance_criteria', 'Acceptance Criteria'], ['validation_plan', 'Validation Plan'],
    ['assumptions', 'Assumptions'], ['out_of_scope', 'Out of Scope'],
    ['handoff_requirements', 'Handoff Requirements'],
  ])
  assert.equal(sections[0].items, criteria)
  assert.deepEqual(sections.slice(1).map(section => section.items), [[], [], [], []])
})

test('Goal Packet gate labels preserve review-request and completion precedence', () => {
  assert.equal(goalPacketGateLabel({}), 'Not recorded')
  const task = { goal_packet: { status: 'pending_review' } }
  assert.equal(goalPacketGateLabel(task), 'Pending approval')
  assert.equal(goalPacketGateLabel({ ...task, review_requested_at: 'requested' }), 'Awaiting Goal Packet approval')
  assert.equal(goalPacketGateLabel({ ...task, review_requested_at: 'requested', review_completed_at: 'done' }), 'Review needs input')
  assert.equal(goalPacketGateLabel({ goal_packet: { status: 'approved' } }), 'Approved for implementation')
  assert.equal(goalPacketGateLabel({ goal_packet: { status: 'rejected' } }), 'Changes requested before implementation')
  for (const [status, label] of [
    [undefined, 'Draft'], ['pending_review', 'Pending review'], ['approved', 'Approved'],
    ['rejected', 'Rejected'], ['frozen', 'Frozen'], ['superseded', 'Superseded'],
  ]) {
    assert.equal(goalPacketStatusLabel(status), label)
  }
})

test('report summary orders acceptance results and pluralizes evidence counts', () => {
  const checks = ['failed', 'passed', 'not_checked', 'partial', 'passed'].map(status => ({ status }))
  const value = report('report', {
    acceptance_check: checks, changed_files: ['one'], validation: 'tested', risks: 'risk',
    profile_results: [{ profile: 'code' }], artifact_refs: ['one.md', 'two.md'],
  })
  assert.equal(acceptanceChecksFor(value), checks)
  assert.equal(reportSummaryLabel(value), '2 passed · 1 partial · 1 failed · 1 not_checked · 1 file · validated · risks noted · 1 review profile · 2 artifacts')
  assert.equal(reportSummaryLabel(report('report', { changed_files: ['a', 'b'], profile_results: [{}, {}], artifact_refs: ['x'] })), '2 files · 2 review profiles · 1 artifact')
  assert.equal(reportSummaryLabel(report('report')), '')
  assert.deepEqual(acceptanceChecksFor(report('report', { acceptance_check: {} })), [])
  assert.deepEqual(profileResultsFor(report('report', { profile_results: null })), [])
})

test('report language honors preferred, secondary, and legacy fields without trimming messages', () => {
  const value = report('report', { message_en: ' English ', message_zh: ' 中文 ' })
  assert.equal(reportMessageForLang(value, 'en'), ' English ')
  assert.equal(reportMessageForLang(value, 'zh'), ' 中文 ')
  assert.equal(reportMessageForLang({ ...value, message_zh: '' }, 'zh'), ' English ')
  assert.equal(reportMessageForLang(report('report', { message_zh: 'Chinese fallback' }), 'en'), 'Chinese fallback')
  assert.equal(reportMessageForLang(report('report'), 'zh'), 'legacy text')
  assert.equal(reportMessageForLang({ ...value, message_zh: ' ' }, 'zh'), ' ')
  assert.equal(hasBilingualReport([report('report', { message_en: ' ', message_zh: '' })]), false)
  assert.equal(hasBilingualReport([value]), true)
  assert.equal(hasBilingualReport([]), false)
})

test('report preview trims only at preview time and truncates after the 120-character boundary', () => {
  assert.equal(reportMessagePreview(report('report', { message: ` ${'a'.repeat(120)} ` }), 'en'), 'a'.repeat(120))
  assert.equal(reportMessagePreview(report('report', { message_en: 'x'.repeat(121), message_zh: '短文' }), 'en'), `${'x'.repeat(117)}…`)
  assert.equal(reportMessagePreview(report('report', { message_en: 'x'.repeat(121), message_zh: '短文' }), 'zh'), '短文')
})

test('primary expansion prefers the latest substantive final report without reordering input', () => {
  const olderFinal = report('final-one', { state: 'completed', validation: 'test' })
  const latestFinal = report('final-two', { state: 'review_passed', evaluation_report: {} })
  const progress = report('progress', { changed_files: ['a'] })
  const emptyFinal = report('empty-final', { state: 'completed' })
  const reports = Object.freeze([olderFinal, latestFinal, progress, emptyFinal])
  assert.equal(primaryExpandedReportId(reports), 'final-two')
  assert.equal(primaryExpandedReportId([progress, emptyFinal]), 'progress')
  assert.equal(primaryExpandedReportId([emptyFinal, report('newest')]), 'newest')
  assert.equal(primaryExpandedReportId([]), null)
  assert.deepEqual(reports.map(value => value.id), ['final-one', 'final-two', 'progress', 'empty-final'])
  for (const state of ['completed', 'ready_for_review', 'review_passed', 'review_failed']) {
    assert.equal(primaryExpandedReportId([report(state, { state, risks: 'risk' }), progress]), state)
  }
  for (const field of ['changed_files', 'acceptance_check', 'profile_results', 'artifact_refs']) {
    assert.equal(primaryExpandedReportId([report(field, { [field]: ['evidence'] }), emptyFinal]), field)
  }
})

test('review profile display prefers explicit task profiles then autonomy policy', () => {
  const profiles = [{ profile: 'code', status: 'passed' }, { profile: 'ui', status: 'failed' }]
  assert.equal(profileResultsFor(report('r', { profile_results: profiles })), profiles)
  assert.equal(profileResultSummary(profiles), 'Code passed · UI failed')
  assert.equal(reviewProfileLabel('future-profile'), 'future-profile')
  assert.equal(taskReviewProfiles({ review_profiles: ['ui'], autonomy_policy: { review_profiles: ['code'] } }), 'UI')
  assert.equal(taskReviewProfiles({ review_profiles: [], autonomy_policy: { review_profiles: ['code', 'boundary'] } }), 'Code, Boundary')
  assert.equal(taskReviewProfiles({ autonomy_policy: { require_artifact_review: true } }), 'Inferred + Artifact')
  assert.equal(taskReviewProfiles({}), 'Inferred')
})

test('Markdown artifacts and preview keys retain path, extension, and source conventions', () => {
  for (const path of [' README.MD ', 'docs/spec.markdown?raw=1', 'notes.mdown#part', 'file.mkd:12']) {
    assert.equal(isMarkdownArtifact(path), true, path)
  }
  for (const path of ['file.txt', 'file.md.backup', '', 'file.md:abc']) {
    assert.equal(isMarkdownArtifact(path), false, path)
  }
  assert.deepEqual(markdownArtifactRefs(report('r', { artifact_refs: ['spec.md', 'image.png'] })), ['spec.md'])
  assert.deepEqual(markdownArtifactRefs(report('r')), [])
  assert.equal(artifactPreviewKey(report('r'), 'spec.md'), 'r:spec.md')
  assert.equal(markdownDocumentPreviewKey({ id: 'one' }), 'doc:one')
  assert.deepEqual(['artifact', 'changed_file', 'snapshot', 'discovered'].map(markdownDocumentSourceLabel), [
    'Official artifact', 'Changed Markdown', 'Workspace snapshot', 'Discovered',
  ])
})

test('task Markdown association uses task or report identity and excludes maintenance documents', () => {
  const direct = { id: 'one', task_id: 't', path: 'direct.md' }
  const viaReport = { id: 'two', report_id: 'r', path: 'report.md' }
  const documents = Object.freeze([
    direct, viaReport,
    { id: 'three', task_id: 'other', path: 'other.md' },
    { id: 'four', task_id: 't', path: 'docs/CHANGELOG.md?raw=1' },
    { id: 'five', report_id: 'r', path: ' changelog.md#history ' },
  ])
  assert.deepEqual(taskMarkdownDocuments({ id: 't' }, [report('r')], documents), [direct, viaReport])
  assert.equal(taskMarkdownDocuments({ id: 't' }, [], documents)[0], direct)
  assert.deepEqual(taskMarkdownDocuments(null, [report('r')], documents), [])
})
