<template>
  <div
    class="conversation-row conversation-row--assistant conversation-row--activity"
  >
    <details
      class="tool-card tool-card--group"
      :class="[`tool-card--${groupStatus()}`, { 'tool-card--single': part.tools.length === 1 }]"
      :open="expandedToolGroups.get(turn.key)?.has(part.key)"
      @toggle="onToolGroupToggle(turn, part.key, $event)"
    >
      <summary
        class="tool-header"
        :class="{ 'tool-header--single': part.tools.length === 1 }"
      >
        <span
          class="activity-icon"
          :data-kind="headline().kind"
          aria-hidden="true"
        ><svg viewBox="0 0 16 16"><path :d="ACTIVITY_ICONS[headline().kind]" /></svg></span>
        <span class="tool-name activity-title">
          <span class="activity-verb">{{ headline().verb }}</span>
          <code
            v-if="headline().target"
            class="activity-target"
          >{{ headline().target }}</code>
        </span>
        <span
          v-if="part.tools.length > 1"
          class="tool-group-names"
        >{{ toolGroupNames(part.tools) }}</span>
        <!-- A failure inside a still-running group must not hide behind
             the spinner. -->
        <span
          v-if="groupStatus() === 'running' && failedCount() > 0"
          class="tool-status failed tool-status--count"
        ><span class="tool-status-text">{{ failedCount() }} failed</span></span>
        <span
          class="tool-status"
          :class="groupStatus()"
        ><span class="tool-status-text">{{ toolStatusLabel(groupStatus()) }}</span></span>
        <span
          class="tool-chevron"
          aria-hidden="true"
        >›</span>
      </summary>
      <template v-if="expandedToolGroups.get(turn.key)?.has(part.key)">
        <div
          v-for="{ tool, activity } in expandedItems()"
          :key="tool.key"
          class="tool-group-item"
        >
          <div
            v-if="part.tools.length > 1"
            class="tool-group-item-header"
          >
            <span
              class="activity-icon activity-icon--small"
              :data-kind="activity.kind"
              aria-hidden="true"
            ><svg viewBox="0 0 16 16"><path :d="ACTIVITY_ICONS[activity.kind]" /></svg></span>
            <span class="tool-name activity-title">
              <span class="activity-verb">{{ activity.verb }}</span>
              <code
                v-if="activity.target"
                class="activity-target"
              >{{ activity.target }}</code>
            </span>
            <span
              class="tool-status"
              :class="tool.status"
            ><span class="tool-status-text">{{ toolStatusLabel(tool.status) }}</span></span>
          </div>
          <!-- Commands read as a terminal: the prompt line, then the output. -->
          <template v-if="activity.command">
            <div class="activity-terminal">
              <pre
                class="activity-terminal-command"
                role="region"
                tabindex="0"
                aria-label="Command"
              ><span
                class="activity-prompt"
                aria-hidden="true"
              >$ </span>{{ activity.command }}</pre>
              <pre
                v-if="tool.resultText"
                class="activity-terminal-output"
                role="region"
                tabindex="0"
                aria-label="Command output"
              >{{ tool.resultText }}</pre>
            </div>
            <details
              v-if="tool.argsText"
              class="tool-block tool-block--raw"
            >
              <summary>Raw input</summary>
              <pre
                tabindex="0"
                aria-label="Tool input"
              >{{ tool.argsText }}</pre>
            </details>
          </template>
          <template v-else>
            <!-- Edits read as a diff when the arguments carry one. Each row
                 keeps a quiet line-number gutter, and a review comment can be
                 pinned to any row so the tray can send the file and line back
                 to the agent instead of a bare snippet. -->
            <div
              v-if="activity.diff"
              :ref="(node) => bindDiffNode(tool.key, node)"
              class="activity-diff"
              role="region"
              tabindex="0"
              aria-label="Edit diff"
            >
              <template
                v-for="(line, index) in activity.diff"
                :key="`${tool.key}-${index}`"
              >
                <div
                  v-if="line.fileHeader"
                  class="activity-diff-file"
                >
                  <span aria-hidden="true">▸</span>
                  <span class="activity-diff-file-name">{{ line.file || line.text }}</span>
                </div>
                <div
                  v-else
                  class="activity-diff-line"
                  :class="[`activity-diff-line--${DIFF_CLASS[line.sign]}`, { 'activity-diff-line--target': isFocusedRow(tool, index) }]"
                  :data-diff-row="index"
                  @click="onRowClick(tool, activity.diff, index, $event)"
                >
                  <button
                    v-if="line.sign !== '@'"
                    type="button"
                    class="activity-diff-add"
                    :aria-label="commentLabel(line)"
                    :title="commentLabel(line)"
                    @click="startComment(tool, activity.diff, index, $event)"
                  >
                    <span aria-hidden="true">+</span>
                  </button>
                  <span
                    v-else
                    class="activity-diff-add activity-diff-add--muted"
                    aria-hidden="true"
                  />
                  <span class="activity-diff-num">{{ rowLine(line) ?? '' }}</span>
                  <span class="activity-diff-sign">{{ line.sign === '@' || line.sign === ' ' ? '' : line.sign }}</span>
                  <span class="activity-diff-text">{{ line.text }}</span>
                </div>
                <div
                  v-for="comment in commentsFor(tool, index)"
                  :key="comment.id"
                  class="activity-diff-note"
                >
                  <span class="activity-diff-note-text">{{ comment.text }}</span>
                  <button
                    type="button"
                    class="activity-diff-note-action"
                    :aria-label="`Edit the review comment on ${reviewLocation(comment)}`"
                    @click="editNote(tool, index, comment)"
                  >
                    Edit
                  </button>
                  <button
                    type="button"
                    class="activity-diff-note-action"
                    :aria-label="`Remove the review comment on ${reviewLocation(comment)}`"
                    @click="discardComment(comment.id)"
                  >
                    Remove
                  </button>
                </div>
                <div
                  v-if="isEditorRow(tool, index)"
                  class="activity-diff-editor"
                >
                  <p class="activity-diff-editor-target">
                    {{ editorLocation() }}
                  </p>
                  <textarea
                    :ref="bindEditorTextarea"
                    v-model="editorDraft"
                    rows="2"
                    :aria-label="`Review comment on ${editorLocation()}`"
                    placeholder="What should change here?"
                    @keydown.enter.exact.prevent="saveComment"
                    @keydown.escape="cancelComment"
                  />
                  <div
                    :ref="bindEditorActions"
                    class="activity-diff-editor-actions"
                  >
                    <button
                      type="button"
                      class="activity-diff-editor-save"
                      :disabled="!editorDraft.trim()"
                      @click="saveComment"
                    >
                      {{ editor?.id ? 'Save' : 'Add comment' }}
                    </button>
                    <button
                      type="button"
                      @click="cancelComment"
                    >
                      Cancel
                    </button>
                  </div>
                </div>
              </template>
            </div>
            <!-- The headline already names the target, so the arguments
                 become secondary evidence. -->
            <details
              v-if="tool.argsText && (activity.diff || activity.target)"
              class="tool-block tool-block--raw"
            >
              <summary>Raw input</summary>
              <pre
                tabindex="0"
                aria-label="Tool input"
              >{{ tool.argsText }}</pre>
            </details>
            <div
              v-else-if="tool.argsText"
              class="tool-block"
            >
              <span>Input</span>
              <pre
                tabindex="0"
                aria-label="Tool input"
              >{{ tool.argsText }}</pre>
            </div>
            <div
              v-if="tool.resultText"
              class="tool-block"
            >
              <span>Result</span>
              <pre
                tabindex="0"
                aria-label="Tool result"
              >{{ tool.resultText }}</pre>
            </div>
          </template>
        </div>
        <button
          type="button"
          class="details-collapse"
          @click="collapseDetails"
        >
          Collapse
        </button>
      </template>
    </details>
    <ChatHubSubagentLink
      v-for="tool in part.tools"
      :key="`hub-subagent-${tool.key}`"
      :args-text="tool.argsText"
      :result-text="tool.resultText"
    />
  </div>
</template>

<script setup lang="ts">
import { requireChatOwner, chatTimelineKey, chatReviewKey } from '../owners'
import { toolGroupNames, toolStatusLabel } from '@/utils/toolPresentation'
import { ACTIVITY_ICONS, describeToolActivity, toolGroupSummary, type ToolActivity, type ToolDiffLine, type ToolGroupSummary } from '@/utils/toolActivity'
import { toolGroupStatus } from '@/features/chat/presentation/chatTurnPresentation'
import { reviewLocation, type ReviewAnchor, type ReviewComment } from '@/features/chat/review/reviewComments'
import ChatHubSubagentLink from './ChatHubSubagentLink.vue'
import type { TimelineTool, TimelineTurn } from '../timeline/types'
import type { TimelinePart } from '../timeline/types'
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
const { expandedToolGroups, onToolGroupToggle, collapseDetails } = requireChatOwner(chatTimelineKey)
const { comments, addComment, removeComment, setAnchorStale, focusTarget } = requireChatOwner(chatReviewKey)
const props = defineProps<{
  turn: TimelineTurn
  part: Extract<TimelinePart, { kind: 'tool_group' }>
}>()

const DIFF_CLASS: Record<ToolDiffLine['sign'], string> = { '+': 'add', '-': 'del', ' ': 'ctx', '@': 'hunk' }

/** One comment per row keeps the diff readable; the row is its identity. */
const commentIndex = computed(() => {
  const index = new Map<string, ReviewComment>()
  for (const comment of comments.value) index.set(comment.key, comment)
  return index
})

interface CommentEditor {
  toolKey: string
  /** Row range the comment covers, in reading order. */
  rows: number[]
  /** Existing comment being edited, when the range already had one. */
  id: string | null
}

const editor = ref<CommentEditor | null>(null)
const editorDraft = ref('')
const editorTextarea = ref<HTMLTextAreaElement | null>(null)
const editorActions = ref<HTMLElement | null>(null)
/** Shift-click extends from the row most recently commented or clicked. */
const lastRow = ref<{ toolKey: string, row: number } | null>(null)
const coarsePointer = ref(false)
const focused = ref<{ toolKey: string, row: number } | null>(null)
const diffNodes = new Map<string, Element>()
let focusTimer: ReturnType<typeof setTimeout> | undefined

onMounted(() => {
  coarsePointer.value = typeof window !== 'undefined' &&
    typeof window.matchMedia === 'function' &&
    window.matchMedia('(pointer: coarse)').matches
})

onUnmounted(() => clearTimeout(focusTimer))

// The tray asks for a row by anchor; a folded card mounts only after the tray
// expanded it, so the row is measured on the next tick.
watch(() => focusTarget.value?.nonce, () => {
  const key = focusTarget.value?.key
  if (!key) return
  // Every rendered card hears the same request; only the one that owns the
  // anchor may judge it, and only that one can report it missing.
  if (!key.startsWith(`${props.part.key}:`)) return
  const split = key.lastIndexOf(':')
  const row = Number(key.slice(split + 1))
  const owner = key.slice(0, split)
  const tool = props.part.tools.find(item => anchorOwner(item) === owner)
  void nextTick(() => {
    const node = tool && Number.isFinite(row)
      ? queryRow(diffNodes.get(tool.key), `[data-diff-row="${row}"]`)
      : null
    if (!tool || !node) {
      // The row this comment was saved against is gone (a history reload
      // renumbers these keys): tell the tray instead of doing nothing.
      setAnchorStale(key, true)
      return
    }
    setAnchorStale(key, false)
    if (typeof node.scrollIntoView === 'function') {
      node.scrollIntoView({ block: 'center', behavior: 'smooth' })
    }
    focused.value = { toolKey: tool.key, row }
    clearTimeout(focusTimer)
    focusTimer = setTimeout(() => { focused.value = null }, 1800)
  })
})

function queryRow(node: Element | undefined, selector: string): Element | null {
  const find = node?.querySelector
  return typeof find === 'function' ? node!.querySelector(selector) : null
}

function bindDiffNode(toolKey: string, node: unknown): void {
  if (node && typeof node === 'object') diffNodes.set(toolKey, node as Element)
  else diffNodes.delete(toolKey)
}

function anchorOwner(tool: TimelineTool): string {
  return `${props.part.key}:${tool.key}`
}

function anchorKey(tool: TimelineTool, row: number): string {
  return `${anchorOwner(tool)}:${row}`
}

/** Preferred number for a row: the new file, else the original file. */
function rowLine(line: ToolDiffLine | undefined): number | null {
  if (!line) return null
  return line.newLine ?? line.oldLine ?? null
}

function rowLocation(line: ToolDiffLine): string {
  const lineNumber = rowLine(line)
  if (!line.file) return lineNumber === null ? '' : `${lineNumber}`
  return lineNumber === null ? line.file : `${line.file}:${lineNumber}`
}

function commentLabel(line: ToolDiffLine): string {
  const where = rowLocation(line)
  return where ? `Add a review comment at ${where}` : 'Add a review comment on this line'
}

function commentsFor(tool: TimelineTool, row: number): ReviewComment[] {
  const comment = commentIndex.value.get(anchorKey(tool, row))
  return comment ? [comment] : []
}

function isFocusedRow(tool: TimelineTool, row: number): boolean {
  return focused.value?.toolKey === tool.key && focused.value.row === row
}

function isEditorRow(tool: TimelineTool, row: number): boolean {
  return editor.value?.toolKey === tool.key && editor.value.rows[0] === row
}

/** Rows between two clicks that share the clicked row's file. */
function selectRows(diff: readonly ToolDiffLine[], anchor: number, target: number): number[] {
  if (anchor === target) return [target]
  const file = diff[target]?.file ?? ''
  const rows: number[] = []
  for (let row = Math.min(anchor, target); row <= Math.max(anchor, target); row++) {
    const line = diff[row]
    if (!line || line.sign === '@') continue
    if ((line.file ?? '') !== file) continue
    rows.push(row)
  }
  return rows.length ? rows : [target]
}

function openEditor(tool: TimelineTool, rows: number[], comment: ReviewComment | null): void {
  const row = rows[0]
  editor.value = { toolKey: tool.key, rows, id: comment?.id ?? null }
  editorDraft.value = comment?.text ?? ''
  lastRow.value = { toolKey: tool.key, row }
  void nextTick(() => {
    // Focus without scrolling, then bring the whole editor into view: a row
    // near the bottom of a tall diff would otherwise leave its buttons below
    // the fold.
    editorTextarea.value?.focus({ preventScroll: true })
    const actions = editorActions.value
    if (actions && typeof actions.scrollIntoView === 'function') {
      actions.scrollIntoView({ block: 'nearest' })
    }
  })
}

/** A string ref inside a ``v-for`` collects into an array, so the editor's
 *  elements are bound explicitly. */
function bindEditorTextarea(node: unknown): void {
  editorTextarea.value = node && typeof node === 'object' ? node as HTMLTextAreaElement : null
}

function bindEditorActions(node: unknown): void {
  editorActions.value = node && typeof node === 'object' ? node as HTMLElement : null
}

function startComment(tool: TimelineTool, diff: readonly ToolDiffLine[] | undefined, row: number, event: MouseEvent): void {
  if (!diff || !diff[row] || diff[row].sign === '@') return
  const previous = lastRow.value?.toolKey === tool.key ? lastRow.value.row : row
  const rows = event.shiftKey ? selectRows(diff, previous, row) : [row]
  openEditor(tool, rows, commentIndex.value.get(anchorKey(tool, rows[0])) ?? null)
}

/** Touch has no hover to reveal the gutter affordance: a tap on the row opens
 *  the editor instead of pressing a fingertip-sized button. */
function onRowClick(tool: TimelineTool, diff: readonly ToolDiffLine[] | undefined, row: number, event: MouseEvent): void {
  if (!coarsePointer.value) return
  if (typeof HTMLButtonElement !== 'undefined' && event.target instanceof HTMLButtonElement) return
  startComment(tool, diff, row, event)
}

function editNote(tool: TimelineTool, row: number, comment: ReviewComment): void {
  openEditor(tool, [row], comment)
}

function discardComment(id: string): void {
  if (editor.value?.id === id) cancelComment()
  removeComment(id)
}

function editorAnchor(): ReviewAnchor | null {
  const state = editor.value
  if (!state) return null
  const tool = props.part.tools.find(item => item.key === state.toolKey)
  if (!tool) return null
  const diff = describeToolActivity(tool).diff ?? []
  const first = diff[state.rows[0]]
  const last = diff[state.rows[state.rows.length - 1]]
  const line = rowLine(first)
  const endLine = rowLine(last)
  return {
    key: anchorKey(tool, state.rows[0]),
    turn: props.turn.key,
    group: props.part.key,
    tool: tool.key,
    row: state.rows[0],
    file: first?.file ?? '',
    line,
    endLine: endLine !== line ? endLine : null,
    code: first?.text ?? '',
  }
}

function editorLocation(): string {
  const anchor = editorAnchor()
  return anchor ? reviewLocation(anchor) : 'diff'
}

function saveComment(): void {
  const anchor = editorAnchor()
  if (!anchor) return
  // Upsert by row: the note and any shifted range land on the same comment.
  addComment(anchor, editorDraft.value)
  editor.value = null
  editorDraft.value = ''
}

function cancelComment(): void {
  editor.value = null
  editorDraft.value = ''
}

// Plain render-time functions, not computeds: the timeline mutates tool rows
// in place and signals change through the turn's renderRevision, which a
// computed cannot observe.
function groupStatus(): ReturnType<typeof toolGroupStatus> {
  return toolGroupStatus(props.part.tools)
}

function failedCount(): number {
  return props.part.tools.reduce((count, tool) => count + (tool.status === 'failed' ? 1 : 0), 0)
}

/** Cached per group, so reading it once per header field costs nothing. */
function headline(): ToolGroupSummary {
  return toolGroupSummary(props.part.tools)
}

/** Arguments are parsed only while the card is open. */
function expandedItems(): { tool: TimelineTool, activity: ToolActivity }[] {
  if (!expandedToolGroups.get(props.turn.key)?.has(props.part.key)) return []
  return props.part.tools.map(tool => ({ tool, activity: describeToolActivity(tool) }))
}
</script>

<style scoped>
/* Tool calls read as a quiet activity log between the prose: one line per
   group ("Ran `pnpm test`"), a card only once someone opens it. */
.conversation-row {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-start;
  min-width: 0;
}

.tool-card {
  width: 100%;
  min-width: 0;
  border: 1px solid transparent;
  border-radius: var(--ch-radius-md);
  background: transparent;
  overflow: clip;
  transition: border-color 160ms ease, background-color 160ms ease, box-shadow 160ms ease;
}

.tool-card[open] {
  border-color: var(--ch-color-border-muted);
  background: var(--ch-color-surface);
  box-shadow: 0 10px 28px var(--ch-shadow-color-soft);
}

.tool-card > summary {
  cursor: pointer;
  list-style: none;
  position: sticky;
  top: 0;
  z-index: 1;
}

.tool-card > summary::-webkit-details-marker {
  display: none;
}

.tool-card[open] > summary {
  background: var(--ch-color-surface);
  border-bottom: 1px solid var(--ch-color-border-muted);
}

.tool-card > summary:focus-visible {
  outline: 2px solid var(--ch-color-accent-ring);
  outline-offset: 2px;
  border-radius: var(--ch-radius-sm);
}

.tool-header {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  min-height: 30px;
  padding: 4px 8px;
  border-radius: var(--ch-radius-md);
  color: var(--ch-color-text-subtle);
  font-size: 13px;
  transition: background-color 120ms ease, color 120ms ease;
}

.tool-card:not([open]) > .tool-header:hover {
  background: var(--ch-color-row-hover, var(--ch-color-surface-soft));
  color: var(--ch-color-text-muted);
}

.tool-card[open] > .tool-header {
  border-radius: 0;
  color: var(--ch-color-text-muted);
}

.tool-name {
  flex-shrink: 0;
  min-width: 0;
  white-space: nowrap;
}

.activity-title {
  display: inline-flex;
  align-items: baseline;
  gap: 6px;
  flex: 0 1 auto;
  min-width: 0;
  overflow: hidden;
}

.tool-header--single .activity-title {
  flex: 1 1 auto;
}

.activity-verb {
  flex: 0 0 auto;
  color: inherit;
  font-weight: 500;
}

.activity-target {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  padding: 1px 6px;
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-surface-sunken, var(--ch-color-surface-soft));
  color: var(--ch-color-text-code, var(--ch-color-text));
  font-family: var(--ch-font-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
  font-size: 12px;
}

.activity-icon {
  width: 20px;
  height: 20px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 auto;
  border-radius: 6px;
  color: var(--ch-color-text-subtle);
}

.activity-icon svg {
  width: 14px;
  height: 14px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.4;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.activity-icon--small {
  width: 16px;
  height: 16px;
}

.activity-icon--small svg {
  width: 12px;
  height: 12px;
}

.activity-icon[data-kind='command'] { color: var(--ch-color-success-strong, var(--ch-color-success)); }
.activity-icon[data-kind='edit'],
.activity-icon[data-kind='write'] { color: var(--ch-color-accent); }
.activity-icon[data-kind='search'],
.activity-icon[data-kind='web'] { color: var(--ch-color-info, var(--ch-color-accent)); }

.tool-card--running .activity-verb {
  background: linear-gradient(
    90deg,
    var(--ch-color-text-subtle) 0%,
    var(--ch-color-text-strong, var(--ch-color-text)) 50%,
    var(--ch-color-text-subtle) 100%
  );
  background-size: 200% 100%;
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
  animation: activity-shimmer 1.8s linear infinite;
}

@keyframes activity-shimmer {
  from { background-position: 100% 0; }
  to { background-position: -100% 0; }
}

.tool-group-names {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  color: var(--ch-color-text-subtle);
  font-family: var(--ch-font-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
  font-size: 11px;
  opacity: 0.75;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* Status stays as text for assistive tech; sighted readers get a glyph,
   and only a failure is loud. */
.tool-status {
  position: relative;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 auto;
  gap: 4px;
  min-width: 16px;
  height: 18px;
  color: var(--ch-color-text-subtle);
  font-size: 11px;
  font-weight: 500;
  white-space: nowrap;
}

.tool-status-text {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip-path: inset(50%);
  white-space: nowrap;
}

.tool-status::before {
  content: '';
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
}

.tool-status.completed {
  color: var(--ch-color-success-strong, #2ea043);
}

.tool-status.completed::before {
  content: '✓';
  width: auto;
  height: auto;
  background: none;
  font-size: 11px;
}

.tool-status.cancelled {
  color: var(--ch-color-text-subtle);
}

.tool-status.cancelled::before {
  width: 7px;
  height: 1.5px;
  border-radius: 1px;
}

.tool-status.running {
  color: var(--ch-color-warning, #e0a800);
}

.tool-status.running::before {
  width: 10px;
  height: 10px;
  border: 1.5px solid currentColor;
  border-right-color: transparent;
  background: none;
  animation: activity-spin 0.8s linear infinite;
}

.tool-status.failed {
  height: 18px;
  padding: 0 7px;
  border-radius: 999px;
  background: var(--ch-color-danger-bg, rgba(248, 81, 73, 0.12));
  color: var(--ch-color-danger-strong, #f85149);
}

.tool-status.failed::before {
  content: '!';
  width: auto;
  height: auto;
  background: none;
  font-weight: 700;
}

.tool-status.failed .tool-status-text {
  position: static;
  width: auto;
  height: auto;
  clip-path: none;
}

@keyframes activity-spin {
  to { transform: rotate(360deg); }
}

.tool-chevron {
  flex: 0 0 auto;
  margin-left: auto;
  color: var(--ch-color-text-subtle);
  font-size: 15px;
  line-height: 1;
  opacity: 0;
  transition: transform 140ms ease, opacity 120ms ease;
}

.tool-header:hover .tool-chevron,
.tool-card[open] > .tool-header .tool-chevron,
.tool-header:focus-visible .tool-chevron {
  opacity: 1;
}

.tool-card[open] > .tool-header .tool-chevron {
  transform: rotate(90deg);
}

.tool-group-item {
  padding: 10px 12px;
  border-top: 1px solid var(--ch-color-border-muted);
}

.tool-group-item:first-of-type {
  border-top: 0;
}

.tool-group-item-header {
  display: flex;
  align-items: center;
  gap: 7px;
  margin-bottom: 8px;
  color: var(--ch-color-text-muted);
  font-size: 12px;
}

.tool-group-item-header .activity-title {
  flex: 1 1 auto;
}

.activity-terminal {
  overflow: hidden;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-sm);
  background: var(--ch-terminal-bg, var(--ch-color-canvas));
}

.activity-terminal pre,
.tool-block pre {
  margin: 0;
  padding: 8px 10px;
  overflow: auto;
  overscroll-behavior: contain;
  font-family: var(--ch-font-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
  font-size: 11.5px;
  line-height: 1.5;
  tab-size: 2;
  white-space: pre;
  word-break: normal;
}

.activity-terminal-command {
  color: var(--ch-terminal-fg, var(--ch-color-text));
  white-space: pre-wrap !important;
  word-break: break-word !important;
}

.activity-prompt {
  color: var(--ch-color-success-strong, var(--ch-color-success));
  user-select: none;
}

.activity-terminal-output {
  max-height: 260px;
  border-top: 1px dashed var(--ch-color-border-muted);
  color: var(--ch-terminal-fg, var(--ch-color-text-code));
  opacity: 0.86;
}

.activity-diff {
  /* A query container so the pinned comment and the editor can be exactly as
   * wide as the visible diff while the code keeps scrolling sideways. */
  container-type: inline-size;
  max-height: 320px;
  overflow: auto;
  overscroll-behavior: contain;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-canvas);
  font-family: var(--ch-font-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
  font-size: 11.5px;
  line-height: 1.5;
}

.activity-diff-file {
  position: sticky;
  left: 0;
  box-sizing: border-box;
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100cqw;
  padding: 3px 8px;
  border-bottom: 1px solid var(--ch-color-border-muted);
  background: var(--ch-color-surface-soft);
  color: var(--ch-color-text-muted);
  font-size: 11px;
  font-weight: 600;
}

.activity-diff-file > span[aria-hidden='true'] {
  color: var(--ch-color-text-subtle);
}

.activity-diff-file-name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.activity-diff-line {
  display: flex;
  align-items: center;
  min-width: max-content;
  padding-right: 10px;
  white-space: pre;
}

.activity-diff-line--target {
  box-shadow: inset 2px 0 0 var(--ch-color-accent);
}

/* The gutter affordance is quiet until a pointer is near the row, and always
 * present for touch and keyboard. */
.activity-diff-add {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 auto;
  width: 18px;
  height: 16px;
  padding: 0;
  border: 0;
  border-radius: 3px;
  background: none;
  color: var(--ch-color-text-subtle);
  font: inherit;
  font-size: 12px;
  line-height: 1;
  opacity: 0;
  cursor: pointer;
}

.activity-diff-add:hover {
  background: var(--ch-color-accent-soft);
  color: var(--ch-color-accent);
}

.activity-diff-add--muted {
  pointer-events: none;
}

.activity-diff-line:hover .activity-diff-add,
.activity-diff-line:focus-within .activity-diff-add {
  opacity: 1;
}

.activity-diff-num {
  flex: 0 0 auto;
  width: 34px;
  padding-right: 6px;
  color: var(--ch-color-text-subtle);
  font-variant-numeric: tabular-nums;
  opacity: 0.65;
  text-align: right;
  user-select: none;
}

.activity-diff-sign {
  width: 22px;
  flex: 0 0 auto;
  text-align: center;
  opacity: 0.7;
  user-select: none;
}

/* A pinned note and its editor sit under the row they belong to and stick to
 * the left edge, so a horizontally scrolled diff still shows them. */
.activity-diff-note,
.activity-diff-editor {
  position: sticky;
  left: 0;
  box-sizing: border-box;
  width: 100cqw;
  padding: 5px 8px 5px 24px;
  border-left: 2px solid var(--ch-color-accent);
  background: var(--ch-color-surface);
  color: var(--ch-color-text);
  font-family: var(--ch-font-sans, ui-sans-serif, system-ui, sans-serif);
  font-size: 12px;
  line-height: 1.45;
  white-space: normal;
}

.activity-diff-note {
  display: flex;
  align-items: flex-start;
  gap: 6px;
}

.activity-diff-note-text {
  flex: 1 1 auto;
  min-width: 0;
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}

.activity-diff-note-action {
  flex: 0 0 auto;
  padding: 0 3px;
  border: 0;
  background: none;
  color: var(--ch-color-text-subtle);
  font: inherit;
  font-size: 11px;
  cursor: pointer;
}

.activity-diff-note-action:hover {
  color: var(--ch-color-text);
}

.activity-diff-editor {
  display: grid;
  gap: 4px;
}

.activity-diff-editor-target {
  margin: 0;
  overflow: hidden;
  color: var(--ch-color-text-subtle);
  font-family: var(--ch-font-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.activity-diff-editor textarea {
  box-sizing: border-box;
  width: 100%;
  padding: 5px 7px;
  resize: vertical;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-app-bg);
  color: var(--ch-color-text);
  font: inherit;
  font-size: 12px;
}

.activity-diff-editor-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.activity-diff-editor-actions button {
  padding: 3px 10px;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-surface);
  color: var(--ch-color-text);
  font: inherit;
  font-size: 11px;
  cursor: pointer;
}

.activity-diff-editor-actions button:disabled {
  opacity: 0.5;
  cursor: default;
}

.activity-diff-editor-actions .activity-diff-editor-save {
  border-color: var(--ch-color-accent-strong);
  background: var(--ch-color-accent-strong);
  color: var(--ch-color-text-inverse);
}

.activity-diff-editor-actions .activity-diff-editor-save:hover:not(:disabled) {
  background: var(--ch-color-accent-fill-hover);
}

.activity-diff-add:focus-visible,
.activity-diff-note-action:focus-visible,
.activity-diff-editor textarea:focus-visible,
.activity-diff-editor-actions button:focus-visible {
  outline: 2px solid var(--ch-color-accent-ring);
  outline-offset: 1px;
}

.activity-diff-line--add {
  background: color-mix(in srgb, var(--ch-code-addition, #2ea043) 14%, transparent);
  color: var(--ch-code-addition, var(--ch-color-success-strong));
}

.activity-diff-line--del {
  background: color-mix(in srgb, var(--ch-code-deletion, #f85149) 12%, transparent);
  color: var(--ch-code-deletion, var(--ch-color-danger-strong));
}

.activity-diff-line--ctx {
  color: var(--ch-color-text-code);
}

.activity-diff-line--hunk {
  padding-top: 3px;
  padding-bottom: 3px;
  background: var(--ch-color-surface-soft);
  color: var(--ch-color-text-subtle);
}

.tool-block {
  margin-top: 8px;
}

.tool-block:first-child {
  margin-top: 0;
}

.tool-block > span,
.tool-block--raw > summary {
  display: block;
  margin-bottom: 5px;
  color: var(--ch-color-text-subtle);
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.05em;
  text-transform: uppercase;
}

.tool-block--raw > summary {
  cursor: pointer;
  list-style: none;
}

.tool-block--raw > summary::-webkit-details-marker {
  display: none;
}

.tool-block--raw > summary::before {
  content: '▸';
  display: inline-block;
  width: 12px;
}

.tool-block--raw[open] > summary::before {
  content: '▾';
}

.tool-block--raw > summary:hover {
  color: var(--ch-color-text-muted);
}

.tool-block pre {
  max-height: 220px;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-canvas);
  color: var(--ch-color-text-code);
}

.tool-block pre:focus-visible,
.activity-terminal pre:focus-visible,
.activity-diff:focus-visible {
  outline: 2px solid var(--ch-color-accent-ring);
  outline-offset: 1px;
}

.details-collapse {
  display: block;
  width: 100%;
  padding: 6px 0;
  border: none;
  border-top: 1px solid var(--ch-color-border-muted);
  background: none;
  color: var(--ch-color-text-subtle);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
}

.details-collapse:hover {
  color: var(--ch-color-text-muted);
}

.details-collapse:focus-visible {
  outline: 2px solid var(--ch-color-accent-ring);
  outline-offset: 2px;
  border-radius: var(--ch-radius-sm);
}

/* Touch: there is no hover to reveal affordances, and a fingertip needs a
   taller target than a pointer. */
@media (hover: none), (pointer: coarse) {
  .tool-chevron {
    opacity: 0.7;
  }

  .tool-header {
    min-height: 40px;
  }

  .tool-block--raw > summary {
    padding: 6px 0;
  }

  /* No hover to reveal the gutter: the row itself is the tap target. */
  .activity-diff-add {
    opacity: 1;
  }

  .activity-diff-note-action,
  .activity-diff-editor-actions button {
    min-height: 44px;
    padding: 4px 8px;
  }
}

@media (max-width: 640px) {
  .tool-group-names {
    display: none;
  }

  .tool-header {
    gap: 7px;
    padding: 4px 6px;
  }

  .tool-group-item {
    padding: 9px 8px;
  }

  .activity-terminal pre,
  .tool-block pre,
  .activity-diff {
    font-size: 11px;
  }

  .activity-terminal-output,
  .tool-block pre {
    max-height: 180px;
  }

  .activity-diff {
    max-height: 260px;
  }

  .activity-diff-add {
    width: 16px;
  }

  .activity-diff-num {
    width: 26px;
  }

  .activity-diff-note,
  .activity-diff-editor {
    padding-left: 14px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .tool-card,
  .tool-chevron,
  .tool-header {
    transition: none;
  }

  .tool-card--running .activity-verb,
  .tool-status.running::before {
    animation: none;
  }
}
</style>
