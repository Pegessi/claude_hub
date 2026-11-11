import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const read = path => readFileSync(new URL(`../src/${path}`, import.meta.url), 'utf8')

const userMessage = read('features/chat/components/ChatUserMessage.vue')
const goalDialog = read('components/GoalSetupDialog.vue')
const goalSetup = read('features/chat/components/ChatGoalSetup.vue')

test('the resend editor accepts a pasted image', () => {
  // Images are staged per surface: the composer's paste handler fills the
  // composer draft, so the inline editor needs its own wiring or a paste into
  // it silently does nothing.
  assert.match(userMessage, /@paste="handleEditPaste"/, 'the edit textarea must forward pastes')
  assert.match(
    userMessage,
    /v-for="att in editAttachments"/,
    'added images render as removable chips so the reader can undo a paste',
  )
  assert.match(userMessage, /@click="removeEditAttachment\(att\)"/, 'each added image can be removed')
})

test('a paste mid-composition does not resend the pre-composition text', () => {
  // Enter confirms an IME candidate; without the guard the confirmation also
  // fires Resend on the half-typed message.
  assert.match(userMessage, /@compositionstart="isEditComposing = true"/)
  assert.match(userMessage, /@keydown\.enter\.exact="handleEditEnter\(\$event, turn\)"/, 'Enter routes through the guard')
  assert.match(userMessage, /if \(event\.isComposing \|\| isEditComposing\.value\) return/)
})

test('the stored-copy warning is rendered before the reader edits', () => {
  assert.match(userMessage, /storedCopyWarning\(props\.turn\.storedTextIncomplete\)/)
  assert.match(userMessage, /v-if="storedCopyWarningText"/)
})

test('the Goal dialog forwards a paste instead of swallowing it', () => {
  // The dialog owns no attachment state; it forwards the raw event so the
  // Chat pane can stage the image with the same pipeline the composer uses.
  assert.match(goalDialog, /@paste="\$emit\('paste', \$event\)"/, 'the objective field must forward pastes')
  assert.match(goalDialog, /paste: \[event: ClipboardEvent\]/, 'the emit is declared in the typed contract')
  assert.match(goalSetup, /@paste="handleGoalPaste"/, 'the pane wires the dialog paste')
  assert.match(
    goalSetup,
    /handlePaste \} = requireChatOwner\(chatAttachmentsKey\)/,
    'the paste uses the composer attachment pipeline, not a second one',
  )
})

test('editing an existing Goal does not stage invisible images', () => {
  // The image picker slot is hidden while editing, so a draft added there
  // could not be seen or removed — but would still ride the next composer send.
  assert.match(goalSetup, /if \(isGoalEditing\.value\) return/)
})
