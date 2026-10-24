import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const readChat = path => readFileSync(new URL(`../src/features/chat/${path}`, import.meta.url), 'utf8')
const pane = readChat('StructuredPane.vue')
const goalController = readChat('controllers/useChatGoal.ts')
const settings = readChat('controllers/useChatSettings.ts')
const queue = readChat('controllers/useChatQueue.ts')
const delivery = readChat('controllers/useChatDelivery.ts')
const attachments = readChat('controllers/useChatAttachments.ts')
const actions = readChat('controllers/useChatActions.ts')
const composer = readChat('components/ChatComposer.vue')
const composerController = readChat('controllers/useChatComposer.ts')
const modePicker = readChat('components/ChatModePicker.vue')
const turn = readChat('components/ChatTurn.vue')
const turnParts = readChat('components/ChatTurnParts.vue')
const composable = readFileSync(new URL('../src/composables/useChatGoal.ts', import.meta.url), 'utf8')
const statusBar = readFileSync(new URL('../src/components/GoalStatusBar.vue', import.meta.url), 'utf8')

test('StructuredPane keeps Goal controls on ordinary Chat rather than managed Task Chat', () => {
  assert.match(pane, /<GoalStatusBar[\s\S]*v-if="goal && !isManagedChat"/)
  assert.match(pane, /<ChatGoalSetup[\s\S]*v-if="!isManagedChat"/)
  assert.doesNotMatch(composable, /agent_type|agentType/)
})

test('Goal details expose the latest checkpoint and invalid-update warning', () => {
  assert.ok(statusBar.includes('Latest checkpoint'))
  assert.ok(statusBar.includes('goal.checkpoint.next_step'))
  assert.ok(statusBar.includes('goal.checkpoint.remaining'))
  assert.ok(statusBar.includes('goal.checkpoint.verified_progress'))
  assert.ok(statusBar.includes('goal.checkpoint_warning'))
})

test('active Goal Plan guard exists at both visible option and action boundary', () => {
  assert.ok(modePicker.includes("option.id === 'plan' && Boolean(goalPlanReason)"))
  assert.ok(settings.includes("modeId === 'plan' && deps.goalPlanReason.value"))
})

test('Goal hydration accepts null and 204 and invalidates stale requests', () => {
  assert.match(composable, /response.status === 204/)
  assert.ok(composable.includes('body ?? null'))
  assert.ok(composable.includes('const requestEpoch = ++epoch'))
  assert.match(composable, /requestEpoch !== epoch/)
  assert.ok(composable.includes('hydrationController?.abort()'))
})

test('Goal hydration cannot invalidate a mutation busy state', () => {
  assert.ok(composable.includes('if (isMutating.value) return'))
  assert.ok(composable.includes('const requestMutationEpoch = ++mutationEpoch'))
  assert.ok(composable.includes('requestMutationEpoch === mutationEpoch'))
})

test('active Goal guards manual history edits and start waits for an idle turn', () => {
  assert.ok(composer.includes(':goal-disabled-reason="goalSetupDisabledReason"'))
  assert.ok(goalController.includes('!isGoalHydrated.value || isGoalHydrating.value'))
  assert.ok(settings.includes('if (deps.modeInteractionLocked.value || isUpdatingMode.value'))
  assert.ok(goalController.includes('goalEditReason'))
})

test('Goal setup and status have no budget or turn-limit controls', () => {
  const dialog = readFileSync(new URL('../src/components/GoalSetupDialog.vue', import.meta.url), 'utf8')
  assert.doesNotMatch(dialog + statusBar + composable, /token_budget|max_turns|updateBudget|budgetDraft/)
  assert.ok(dialog.includes("emit('submit', { objective: trimmed })"))
  assert.match(composer, /<ComposerAddMenu/)
  assert.doesNotMatch(composer, /composer-goal-btn|composer-attach-btn/)
})

test('active Goal also blocks implementing a historical Plan turn', () => {
  assert.ok(turn.includes('implementingPlanKey !== null || turnInFlight || goalComposerLocked'))
  assert.match(
    actions,
    /async function implementPlan[\s\S]*if \(deps\.goalComposerLocked\.value\)/,
  )
})

test('turn completion uses bounded version-aware Goal reconciliation', () => {
  assert.ok(goalController.includes('GOAL_REFRESH_DELAYS_MS'))
  assert.ok(goalController.includes('goalSnapshotVersion() !== baselineVersion'))
  assert.ok(goalController.includes("latest[index].type === 'turn_completed'"))
  assert.ok(goalController.includes('current.completed_turn_ids?.includes(turnId)'))
})

test('active Goal accepts steers while queued drafts wait for the Goal to stop', () => {
  assert.ok(goalController.includes('goalBlocksPlan(goal.value)'))
  assert.ok(delivery.includes("deps.goal.value?.status === 'active'"))
  assert.match(composer, /:disabled="isSending \|\| isGoalMutating \|\| connectionState !== 'live' \|\| goalInputBlocked"/)
  assert.match(
    queue,
    /async function flushDraftQueue\(\)[\s\S]*?deps\.isSubmissionReady\.value[\s\S]*?!isDraftQueuePaused\.value[\s\S]*?!deps\.goalComposerLocked\.value/,
  )
  assert.ok(delivery.includes('if (!messageOverride && deps.goalInputBlocked.value)'))
  // Enter queues and ⌘/Ctrl+Enter steers while the Goal runs.
  assert.ok(composerController.includes('deps.composerTurnInFlight.value || deps.goalSteerOnly.value'))
  assert.match(
    delivery,
    /if \(\s*!deps.isManagedChat\.value &&\s*delivery === 'normal' &&\s*\(composerTurnInFlight\.value \|\| deps\.goalSteerOnly\.value\) &&\s*!messageOverride\s*\) \{\s*deps\.enqueueDraft\(\)/,
  )
  // Direct Goal turns keep steering; managed Task turns always use normal delivery.
  assert.ok(composer.includes("submit(isManagedChat ? 'normal' : (goalSteerOnly ? 'steer' : 'normal'))"))
  assert.ok(composer.includes('⌘/Ctrl+Enter steer Goal'))
  // Images use the same provider original and display-preview pipeline.
  assert.ok(goalController.includes('imageSnapshot.map(toStreamSendAttachment)'))
  assert.match(attachments, /async function addFiles\([\s\S]*?if \(deps\.goalInputBlocked\.value\)/)

})

test('assistant text hides an unsanitized trailing Goal envelope', () => {
  assert.ok(turnParts.includes(':text="renderAgentDirectives(stripTrailingGoalProtocol(part.text), { complete: turn.completed })"'))
})

test('expanded Goal details have a visible collapse control and stay above the status bar', () => {
  assert.match(
    statusBar,
    /class="goal-status-details-close"[\s\S]*aria-label="Collapse Goal details"[\s\S]*@click="expanded = false"/,
  )
  assert.match(statusBar, /\.goal-status \{ position: relative/)
  assert.match(statusBar, /\.goal-status-details \{[\s\S]*bottom: calc\(100% \+ 8px\)/)
})

test('phones get one tappable Goal row and a bottom sheet with pinned actions', () => {
  // One action list feeds the desktop row and the phone sheet.
  assert.match(statusBar, /v-for="action in actions"[\s\S]*v-for="action in actions"/)
  assert.ok(statusBar.includes("const PHONE_QUERY = '(max-width: 640px)'"))
  assert.ok(statusBar.includes('<Teleport'))
  assert.ok(statusBar.includes(':disabled="!isPhone"'))
  assert.match(statusBar, /\.goal-status-details-body \{[^}]*overflow-y: auto/)
  assert.match(statusBar, /\.goal-sheet-actions \{[^}]*flex: 0 0 auto/)
  assert.match(statusBar, /\.goal-sheet-actions button \{[^}]*min-height: 44px/)
  assert.ok(statusBar.includes('env(safe-area-inset-bottom)'))
})

test('Goal status disclosure exposes accessible state and controls', () => {
  assert.ok(statusBar.includes('aria-live="polite"'))
  assert.ok(statusBar.includes(':aria-controls="detailsId"'))
  assert.ok(statusBar.includes('@keydown.esc="expanded = false"'))
  assert.ok(statusBar.includes('@media (pointer: coarse)'))
})

test('Goal errors remain visible and setup traps focus', () => {
  assert.ok(statusBar.includes('error && !expanded'))
  const dialog = readFileSync(new URL('../src/components/GoalSetupDialog.vue', import.meta.url), 'utf8')
  assert.ok(dialog.includes('@keydown.tab="trapFocus"'))
  assert.ok(dialog.includes("if (!props.busy) emit('close')"))
})
