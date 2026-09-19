<template>
  <!--
    Shared per-session actions menu (⋯) used by both the TabBar and the chat
    sidebar, so every session surface offers the same operations:

      Rename · Duplicate · Archive · Copy Link · Switch Env / Model…

    Only the trigger is rendered inline; the popover is teleported to <body>
    so it escapes any overflow clipping in the parent. The Switch Env modal
    also lives here — it is identical regardless of where it was opened.

    Public interface:
      tab          the session this menu acts on
      variant      'tabbar' (default) keeps the trigger hidden until the
                   parent row is hovered/active; 'sidebar' reveals it on row
                   hover via the parent's :deep() rule
    Emits:
      rename       parent owns inline rename UI; menu just closes and emits
  -->
  <button
    ref="triggerRef"
    type="button"
    :class="['tam-trigger', `tam-trigger--${variant}`]"
    :aria-label="`${tab.name} actions`"
    :aria-expanded="open"
    title="Session actions"
    @click.stop="toggle"
  >
    <slot name="trigger">
      ⋯
    </slot>
  </button>

  <!-- Popover teleported to body to escape row/container overflow clipping. -->
  <Teleport to="body">
    <div
      v-if="open"
      ref="panelRef"
      class="tam-panel"
      role="menu"
      :style="panelStyle"
    >
      <button
        v-if="tab.session_kind === 'chat' && !tab.workspace_id"
        type="button"
        class="tam-item"
        role="menuitem"
        title="Saved in this browser"
        @click="handleTogglePin"
      >
        <span
          class="tam-item-icon"
          aria-hidden="true"
        >⌖</span>
        <span>{{ store.pinnedChatIds.has(tab.id) ? 'Unpin session' : 'Pin session' }}</span>
      </button>
      <template v-if="variant === 'sidebar'">
        <button
          type="button"
          class="tam-item"
          role="menuitem"
          :disabled="moveUpDisabled"
          @click="handleMove(-1)"
        >
          <span
            class="tam-item-icon"
            aria-hidden="true"
          >↑</span>
          <span>Move up</span>
        </button>
        <button
          type="button"
          class="tam-item"
          role="menuitem"
          :disabled="moveDownDisabled"
          @click="handleMove(1)"
        >
          <span
            class="tam-item-icon"
            aria-hidden="true"
          >↓</span>
          <span>Move down</span>
        </button>
      </template>
      <button
        type="button"
        class="tam-item"
        role="menuitem"
        @click="handleRename"
      >
        <span
          class="tam-item-icon"
          aria-hidden="true"
        >✎</span>
        <span>Rename</span>
      </button>
      <LoadingButton
        type="button"
        class="tam-item"
        role="menuitem"
        :loading="isPending(actionKey('duplicate'))"
        loading-label="Duplicating…"
        @click="handleDuplicate"
      >
        <span
          class="tam-item-icon"
          aria-hidden="true"
        >📋</span>
        <span>Duplicate</span>
      </LoadingButton>
      <LoadingButton
        type="button"
        class="tam-item"
        role="menuitem"
        :loading="isPending(actionKey('archive'))"
        loading-label="Archiving…"
        @click="handleArchive"
      >
        <span
          class="tam-item-icon"
          aria-hidden="true"
        >🗄</span>
        <span>Archive</span>
      </LoadingButton>
      <button
        type="button"
        class="tam-item"
        role="menuitem"
        @click="handleCopyLink"
      >
        <span
          class="tam-item-icon"
          aria-hidden="true"
        >🔗</span>
        <span>Copy Link</span>
      </button>
      <LoadingButton
        v-if="tab.agent_type === 'claude' || tab.agent_type === 'codex' || (tab.agent_type === 'traex' && tab.session_kind === 'chat')"
        type="button"
        class="tam-item"
        role="menuitem"
        :loading="isPending(actionKey('switch-env'))"
        loading-label="Switching env…"
        @click="openSwitchEnvModal"
      >
        <span
          class="tam-item-icon"
          aria-hidden="true"
        >⚙</span>
        <span>Switch Env / Model…</span>
      </LoadingButton>
    </div>
  </Teleport>

  <!-- Switch Env Modal. dragstart is stopped so dragging from modal chrome
       cannot initiate the underlying tab's drag (the trigger may live inside
       a draggable .tab). -->
  <div
    v-if="showSwitchEnv"
    class="tam-overlay"
    @click.self="closeSwitchEnvModal"
    @dragstart.stop
  >
    <div class="tam-modal tam-switch-env-modal">
      <div class="tam-switch-env-header">
        <div
          class="tam-switch-env-icon"
          aria-hidden="true"
        >
          ⚙
        </div>
        <div class="tam-switch-env-title-block">
          <h3>Switch Environment</h3>
          <p class="tam-switch-env-subtitle">
            {{ tab.name }}
          </p>
        </div>
      </div>
      <p class="tam-switch-env-callout">
        <span
          class="tam-switch-env-callout-icon"
          aria-hidden="true"
        >↻</span>
        <span>
          The chat provider will restart and automatically resume this conversation.
          In-flight generation will be interrupted.
        </span>
      </p>
      <form @submit.prevent="handleSwitchEnv">
        <div class="tam-form-group tam-env-editor">
          <label>Environment Preset</label>
          <div class="tam-env-preset-row">
            <select
              v-model="switchEnvForm.env_preset"
              class="tam-select-input"
              @change="applySwitchEnvPreset(switchEnvForm.env_preset)"
            >
              <option
                v-for="preset in envPresets"
                :key="preset.id"
                :value="preset.id"
              >
                {{ preset.name }}
              </option>
              <option value="custom">
                Custom (current values)
              </option>
            </select>
            <button
              type="button"
              class="ch-btn ch-btn--sm tam-env-manage-button"
              @click="openSwitchEnvPresetManager"
            >
              Manage
            </button>
          </div>
        </div>
        <div class="tam-form-group">
          <label for="tam-switch-env-text">
            Environment Variables
            <span class="tam-field-hint-inline">(KEY=VALUE, one per line)</span>
          </label>
          <textarea
            id="tam-switch-env-text"
            v-model="switchEnvForm.env_text"
            class="tam-select-input tam-env-textarea"
            rows="6"
            placeholder="ANTHROPIC_MODEL=claude-sonnet-4-5&#10;ANTHROPIC_BASE_URL=https://..."
          />
          <p class="tam-form-hint">
            These fully replace the tab's current environment. Include
            <code>ANTHROPIC_MODEL</code> to switch models.
          </p>
        </div>
        <div class="tam-form-group">
          <label class="tam-checkbox-label">
            <div class="tam-checkbox-row">
              <input
                v-model="switchEnvForm.solo_mode"
                type="checkbox"
                class="tam-checkbox-input"
              >
              <span class="tam-checkbox-text">Solo Mode</span>
            </div>
            <span
              v-if="tab.agent_type === 'codex' || tab.agent_type === 'traex'"
              class="tam-checkbox-desc"
            >
              Relaunch with <code>--ask-for-approval never</code> and
              <code>--sandbox danger-full-access</code>.
            </span>
            <span
              v-else
              class="tam-checkbox-desc"
            >
              Relaunch with <code>IS_SANDBOX=1</code> and
              <code>--dangerously-skip-permissions</code>.
            </span>
          </label>
        </div>
        <div class="tam-modal-actions">
          <button
            type="button"
            class="ch-btn"
            @click="closeSwitchEnvModal"
          >
            Cancel
          </button>
          <LoadingButton
            type="submit"
            class="ch-btn ch-btn--primary tam-switch-env-submit"
            :loading="isPending(actionKey('switch-env'))"
            loading-label="Restarting…"
          >
            Restart Provider
          </LoadingButton>
        </div>
      </form>
    </div>
  </div>

  <!-- Mounted lazily: there is one menu per session row, and the manager sets
       up its own watchers/state, so it should exist only while actually open.
       Close applies the chosen preset before this unmounts. -->
  <EnvPresetManager
    v-if="showSwitchEnvManager"
    v-model:model-value="switchEnvForm.env_preset"
    :visible="showSwitchEnvManager"
    @close="closeSwitchEnvPresetManager"
  />
</template>

<script setup lang="ts">
import { computed, nextTick, onUnmounted, reactive, ref, watch } from 'vue'
import type { CSSProperties } from 'vue'
import LoadingButton from '@/components/LoadingButton.vue'
import EnvPresetManager from '@/components/EnvPresetManager.vue'
import { parseLaunchEnv, useLaunchEnvPresets } from '@/composables/useLaunchEnvPresets'
import { usePendingActions } from '@/composables/usePendingActions'
import { useTerminalStore } from '@/stores/terminalStore'
import { writeClipboard } from '@/utils/clipboard'
import { buildTabShareText } from '@/utils/deepLink'
import type { SwitchEnvRequest, TerminalTab } from '@/types'

interface Props {
  tab: TerminalTab
  variant?: 'tabbar' | 'sidebar'
  moveUpDisabled?: boolean
  moveDownDisabled?: boolean
}

const props = withDefaults(defineProps<Props>(), {
  variant: 'tabbar',
  moveUpDisabled: true,
  moveDownDisabled: true,
})

const emit = defineEmits<{
  (e: 'rename', tab: TerminalTab): void
  (e: 'move', direction: -1 | 1): void
}>()

const store = useTerminalStore()
const { envPresets, getPresetText } = useLaunchEnvPresets()
const { isPending, runPending } = usePendingActions()

// ---- Popover ----
const open = ref(false)
const triggerRef = ref<HTMLButtonElement | null>(null)
const panelRef = ref<HTMLElement | null>(null)
// Bumped on scroll/resize while open so the fixed-position panel re-measures.
const positionTick = ref(0)

const panelStyle = computed<CSSProperties>(() => {
  // Depend on `open` (re-measure every time the popover opens) and the tick
  // (re-measure on scroll/resize); getBoundingClientRect itself is non-reactive.
  void open.value
  void positionTick.value
  const trigger = triggerRef.value
  if (!trigger) return {}
  const rect = trigger.getBoundingClientRect()
  // Align the panel's right edge with the trigger; place it just below with a gap.
  const panelWidth = 200
  const panelHeightEst = props.variant === 'sidebar' ? 310 : 240
  let top = rect.bottom + 6
  let left = rect.right - panelWidth
  const vw = window.innerWidth
  const vh = window.innerHeight
  if (left < 8) left = 8
  if (left + panelWidth > vw - 8) left = vw - panelWidth - 8
  if (top + panelHeightEst > vh - 8) top = Math.max(8, rect.top - panelHeightEst - 6)
  return {
    position: 'fixed',
    top: `${top}px`,
    left: `${left}px`,
    width: `${panelWidth}px`,
    maxHeight: `${Math.max(80, vh - 16)}px`,
    overflowY: 'auto',
  }
})

function reposition() {
  positionTick.value += 1
}

function toggle() {
  if (open.value) {
    close()
  } else {
    // Tell every other menu instance to close first (there is one menu per
    // session row), then open this one.
    window.dispatchEvent(
      new CustomEvent('claude-hub:tab-menu-opened', { detail: { source: triggerRef.value } })
    )
    open.value = true
  }
}

function close() {
  open.value = false
}

function handleOtherMenuOpened(event: Event) {
  if (!open.value) return
  const detail = (event as CustomEvent<{ source?: HTMLElement }>).detail
  if (detail?.source !== triggerRef.value) close()
}

function actionKey(action: string) {
  return `tab:${props.tab.id}:${action}`
}

function handleDocumentPointerDown(event: PointerEvent) {
  if (!open.value) return
  const target = event.target
  if (!(target instanceof Node)) return
  const inTrigger = triggerRef.value?.contains(target)
  const inPanel = panelRef.value?.contains(target)
  if (!inTrigger && !inPanel) close()
}

function handleDocumentKeyDown(event: KeyboardEvent) {
  if (event.key === 'Escape') close()
}

// One menu exists per session row, so global listeners are bound only while
// this menu's popover is open (not once per instance for the app's lifetime).
// Scroll is captured so scrolling inside any container (the sidebar list, the
// tab strip) still repositions the fixed panel.
watch(open, (isOpen) => {
  if (isOpen) {
    document.addEventListener('pointerdown', handleDocumentPointerDown)
    document.addEventListener('keydown', handleDocumentKeyDown)
    window.addEventListener('resize', reposition)
    window.addEventListener('scroll', reposition, true)
    window.addEventListener('claude-hub:tab-menu-opened', handleOtherMenuOpened)
  } else {
    document.removeEventListener('pointerdown', handleDocumentPointerDown)
    document.removeEventListener('keydown', handleDocumentKeyDown)
    window.removeEventListener('resize', reposition)
    window.removeEventListener('scroll', reposition, true)
    window.removeEventListener('claude-hub:tab-menu-opened', handleOtherMenuOpened)
  }
})

onUnmounted(() => {
  document.removeEventListener('pointerdown', handleDocumentPointerDown)
  document.removeEventListener('keydown', handleDocumentKeyDown)
  window.removeEventListener('resize', reposition)
  window.removeEventListener('scroll', reposition, true)
  window.removeEventListener('claude-hub:tab-menu-opened', handleOtherMenuOpened)
})

// ---- Menu actions ----
function handleTogglePin() {
  close()
  store.setChatPinned(props.tab.id, !store.pinnedChatIds.has(props.tab.id))
}

function handleMove(direction: -1 | 1) {
  close()
  emit('move', direction)
}

function handleRename() {
  close()
  emit('rename', props.tab)
}

async function handleDuplicate() {
  close()
  await runPending(actionKey('duplicate'), () => store.duplicateTab(props.tab.id))
}

async function handleArchive() {
  close()
  await runPending(actionKey('archive'), () => store.archiveTab(props.tab.id))
}

async function handleCopyLink() {
  close()
  try {
    await writeClipboard(buildTabShareText(props.tab.id))
    store.pushNotification({
      type: 'success',
      message: 'Link copied',
      autoDismissMs: 3000,
    })
  } catch {
    store.pushNotification({
      type: 'error',
      message: 'Failed to copy link',
      autoDismissMs: 8000,
    })
  }
}

// ---- Switch Env ----
const showSwitchEnv = ref(false)
const showSwitchEnvManager = ref(false)
const switchEnvForm = reactive({
  env_preset: 'custom' as string,
  env_text: '',
  solo_mode: false,
})

function serializeEnv(env: Record<string, string> | undefined): string {
  if (!env) return ''
  return Object.entries(env)
    .map(([k, v]) => `${k}=${v}`)
    .join('\n')
}

async function openSwitchEnvModal() {
  close()
  // Let the popover unmount/repaint before the overlay captures pointer events.
  await nextTick()
  switchEnvForm.env_preset = 'custom'
  switchEnvForm.env_text = serializeEnv(props.tab.env)
  switchEnvForm.solo_mode = props.tab.solo_mode ?? false
  showSwitchEnv.value = true
}

function closeSwitchEnvModal() {
  showSwitchEnv.value = false
  showSwitchEnvManager.value = false
}

function applySwitchEnvPreset(presetId: string) {
  if (presetId === 'custom') return
  const text = getPresetText(presetId)
  if (text === null) return
  switchEnvForm.env_text = text
}

function openSwitchEnvPresetManager() {
  showSwitchEnvManager.value = true
}

function closeSwitchEnvPresetManager() {
  showSwitchEnvManager.value = false
  applySwitchEnvPreset(switchEnvForm.env_preset)
}

async function handleSwitchEnv() {
  const env = parseLaunchEnv(switchEnvForm.env_text)
  if (!env) {
    store.pushNotification({
      type: 'error',
      message: 'Please provide at least one KEY=VALUE environment variable, or pick a preset.',
      autoDismissMs: 6000,
    })
    return
  }
  const payload: SwitchEnvRequest = {
    env,
    solo_mode: switchEnvForm.solo_mode,
  }
  const tab = props.tab
  try {
    await runPending(actionKey('switch-env'), async () => {
      await store.switchEnv(tab.id, payload)
    })
    store.pushNotification({
      type: 'success',
      message: `Environment switched for "${tab.name}". Chat is resuming its conversation.`,
      autoDismissMs: 4000,
    })
    closeSwitchEnvModal()
  } catch (e) {
    // switchEnv already notifies; log for visibility.
    console.error('switch env failed', e)
  }
}
</script>

<style scoped>
/* Trigger: base styling is shared; the reveal-on-hover behavior is owned by
   each parent (TabBar / ChatSidebar) via :deep(), since only the parent knows
   when its row is hovered. */
.tam-trigger {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: none;
  border: none;
  color: var(--ch-color-text-soft);
  font-size: 16px;
  line-height: 1;
  padding: 2px 6px;
  border-radius: var(--ch-radius-sm);
  cursor: pointer;
  transition:
    color var(--ch-motion-fast),
    background var(--ch-motion-fast);
}

.tam-trigger--sidebar {
  width: 22px;
  height: 22px;
  padding: 0;
  font-size: 14px;
}

.tam-trigger:hover,
.tam-trigger[aria-expanded='true'] {
  color: var(--ch-color-text);
  background: var(--ch-color-chip-bg);
}

.tam-trigger:focus-visible {
  outline: none;
  box-shadow: 0 0 0 3px var(--ch-color-accent-ring);
}

/* Popover */
.tam-panel {
  z-index: 1200;
  padding: 6px;
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-raised);
  border: 1px solid var(--ch-color-border);
  box-shadow: var(--ch-shadow-popover);
  display: flex;
  flex-direction: column;
  gap: 2px;
  animation: tam-menu-in var(--ch-motion-fast);
  transform-origin: top right;
}

@keyframes tam-menu-in {
  from {
    opacity: 0;
    transform: translateY(-4px) scale(0.98);
  }
  to {
    opacity: 1;
    transform: translateY(0) scale(1);
  }
}

.tam-item {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  text-align: left;
  background: transparent;
  border: none;
  color: var(--ch-color-text);
  font-size: var(--ch-font-size-sm);
  font-weight: 500;
  line-height: 1.2;
  padding: 8px 10px;
  border-radius: var(--ch-radius-md);
  cursor: pointer;
  transition: background var(--ch-motion-fast), color var(--ch-motion-fast);
}

.tam-item:hover {
  background: var(--ch-color-row-hover);
}

.tam-item:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.tam-item:focus-visible {
  outline: none;
  box-shadow: 0 0 0 3px var(--ch-color-accent-ring);
}

.tam-item-icon {
  flex: 0 0 auto;
  width: 16px;
  text-align: center;
  color: var(--ch-color-text-muted);
  font-size: var(--ch-font-size-sm);
}

/* ---- Switch Env modal (self-contained; mirrors the old TabBar modal) ---- */
.tam-overlay {
  position: fixed;
  inset: 0;
  background-color: var(--ch-color-overlay-soft);
  display: flex;
  align-items: center;
  justify-content: center;
  box-sizing: border-box;
  padding: 16px;
  overflow-y: auto;
  -webkit-overflow-scrolling: touch;
  z-index: 1100;
}

.tam-modal {
  background-color: var(--ch-color-surface);
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-lg);
  padding: 24px;
  width: min(480px, calc(100vw - 32px));
  max-width: 100%;
  max-height: calc(100dvh - 32px);
  overflow-y: auto;
  overscroll-behavior: contain;
  -webkit-overflow-scrolling: touch;
  display: flex;
  flex-direction: column;
  box-sizing: border-box;
}

.tam-switch-env-header {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
}

.tam-switch-env-icon {
  flex: 0 0 auto;
  width: 36px;
  height: 36px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-accent-soft);
  color: var(--ch-color-accent);
  font-size: 18px;
}

.tam-switch-env-title-block {
  min-width: 0;
}

.tam-switch-env-title-block h3 {
  margin: 0;
  font-size: var(--ch-font-size-lg);
  color: var(--ch-color-text);
}

.tam-switch-env-subtitle {
  margin: 2px 0 0;
  font-size: var(--ch-font-size-sm);
  color: var(--ch-color-text-muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.tam-switch-env-callout {
  display: flex;
  gap: 10px;
  align-items: flex-start;
  margin: 0 0 18px;
  padding: 10px 12px;
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-soft, rgba(255, 255, 255, 0.04));
  border: 1px solid var(--ch-color-border-muted);
  border-left: 3px solid var(--ch-color-accent);
  font-size: var(--ch-font-size-sm);
  line-height: 1.5;
  color: var(--ch-color-text-muted);
}

.tam-switch-env-callout-icon {
  flex: 0 0 auto;
  color: var(--ch-color-accent);
  font-weight: 600;
  line-height: 1.5;
}

.tam-field-hint-inline {
  color: var(--ch-color-text-soft);
  font-weight: 400;
}

.tam-form-group {
  margin-bottom: 16px;
  position: relative;
}

.tam-form-group label {
  display: block;
  color: var(--ch-color-text);
  margin-bottom: 6px;
  font-size: var(--ch-font-size-md);
}

.tam-select-input {
  width: 100%;
  padding: 0 12px;
  background-color: var(--ch-color-surface-control);
  border: 1px solid var(--ch-color-border-strong);
  border-radius: var(--ch-radius-md);
  color: var(--ch-color-text);
  font-size: var(--ch-font-size-md);
  box-sizing: border-box;
}

.tam-select-input:hover {
  border-color: var(--ch-color-border-hover);
}

.tam-select-input:focus {
  outline: none;
  border-color: var(--ch-color-accent);
  box-shadow: 0 0 0 3px var(--ch-color-accent-ring);
}

.tam-env-editor {
  gap: 8px;
}

.tam-env-preset-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 8px;
}

.tam-env-preset-row .tam-select-input {
  height: 36px;
}

.tam-env-manage-button {
  white-space: nowrap;
}

.tam-env-textarea {
  min-height: 92px;
  padding: 10px 12px;
  resize: vertical;
  font-family: var(--ch-font-mono);
  line-height: 1.45;
}

.tam-form-hint {
  color: var(--ch-color-text-soft);
  font-size: var(--ch-font-size-sm);
  margin: 6px 0 0;
}

.tam-form-group label code,
.tam-checkbox-desc code,
.tam-form-hint code {
  font-family: var(--ch-font-mono);
  font-size: var(--ch-font-size-sm);
  padding: 1px 5px;
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text);
}

.tam-checkbox-label {
  display: flex;
  flex-direction: column;
  gap: 4px;
  cursor: pointer;
}

.tam-checkbox-row {
  display: flex;
  align-items: center;
}

.tam-checkbox-input {
  margin-right: 8px;
  width: 16px;
  height: 16px;
  cursor: pointer;
  accent-color: var(--ch-color-accent);
}

.tam-checkbox-text {
  color: var(--ch-color-text);
  font-size: var(--ch-font-size-md);
  font-weight: 500;
}

.tam-checkbox-desc {
  color: var(--ch-color-text-soft);
  font-size: var(--ch-font-size-sm);
  margin-left: 24px;
}

.tam-switch-env-submit {
  min-width: 124px;
}

.tam-modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
  margin-top: 24px;
}

@media (max-width: 640px) {
  .tam-overlay {
    align-items: flex-start;
    padding: 10px;
  }

  .tam-modal {
    width: 100%;
    max-height: calc(100dvh - 20px);
    padding: 16px;
    border-radius: var(--ch-radius-md);
  }

  .tam-modal-actions {
    position: sticky;
    bottom: -1px;
    background-color: var(--ch-color-surface);
    padding-top: 12px;
  }

  .tam-modal-actions .ch-btn {
    flex: 1;
  }
}
</style>
