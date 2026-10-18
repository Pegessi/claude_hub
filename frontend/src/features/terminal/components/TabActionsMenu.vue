<template>
  <!--
    Shared per-session actions menu (⋯) used by both the TabBar and the chat
    sidebar, so every session surface offers the same operations:

      Rename · Duplicate · Archive · Copy Link · Switch Env / Model… · Delete

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
    v-bind="$attrs"
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
        title="Saved on this Hub"
        @click="handleTogglePin"
      >
        <span
          class="tam-item-icon"
          aria-hidden="true"
        >⌖</span>
        <span>{{ store.pinnedChatIds.has(tab.id) ? 'Unpin session' : 'Pin session' }}</span>
      </button>
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
      <div
        class="tam-divider"
        role="separator"
      />
      <button
        type="button"
        class="tam-item tam-item--danger"
        role="menuitem"
        @click="openDeleteConfirm"
      >
        <span
          class="tam-item-icon"
          aria-hidden="true"
        >⌫</span>
        <span>Delete</span>
      </button>
    </div>
  </Teleport>

  <Teleport to="body">
    <div
      v-if="showDeleteConfirm"
      class="tam-overlay tam-delete-overlay"
      role="presentation"
      @click.self="closeDeleteConfirm"
      @keydown.esc="closeDeleteConfirm"
      @dragstart.stop
    >
      <div
        class="tam-modal tam-delete-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="tam-delete-title"
        aria-describedby="tam-delete-description"
      >
        <div
          class="tam-delete-icon"
          aria-hidden="true"
        >
          ⌫
        </div>
        <h3 id="tam-delete-title">
          Delete session?
        </h3>
        <p
          id="tam-delete-description"
          class="tam-delete-description"
        >
          “{{ tab.name || 'Untitled' }}” and its conversation history will be
          permanently deleted. This cannot be undone.
        </p>
        <div class="tam-modal-actions">
          <button
            ref="deleteCancelRef"
            type="button"
            class="ch-btn"
            @click="closeDeleteConfirm"
          >
            Cancel
          </button>
          <LoadingButton
            type="button"
            class="ch-btn ch-btn--danger"
            :loading="isPending(actionKey('delete'))"
            loading-label="Deleting session"
            @click="handleDelete"
          >
            Delete permanently
          </LoadingButton>
        </div>
      </div>
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

defineOptions({ inheritAttrs: false })

interface Props {
  tab: TerminalTab
  variant?: 'tabbar' | 'sidebar'
}

const props = withDefaults(defineProps<Props>(), {
  variant: 'tabbar',
})

const emit = defineEmits<{
  (e: 'rename', tab: TerminalTab): void
}>()

const store = useTerminalStore()
const { envPresets, getPresetText } = useLaunchEnvPresets()
const { isPending, runPending } = usePendingActions()

// ---- Popover ----
const open = ref(false)
const triggerRef = ref<HTMLButtonElement | null>(null)
const panelRef = ref<HTMLElement | null>(null)
const deleteCancelRef = ref<HTMLButtonElement | null>(null)
const showDeleteConfirm = ref(false)
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
  const panelHeightEst = 280
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
async function handleTogglePin() {
  close()
  await store.setChatPinned(props.tab.id, !store.pinnedChatIds.has(props.tab.id))
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

async function openDeleteConfirm() {
  close()
  await nextTick()
  showDeleteConfirm.value = true
  await nextTick()
  deleteCancelRef.value?.focus()
}

async function closeDeleteConfirm() {
  if (isPending(actionKey('delete'))) return
  showDeleteConfirm.value = false
  await nextTick()
  triggerRef.value?.focus()
}

async function handleDelete() {
  const deleted = await runPending(actionKey('delete'), () => store.deleteTab(props.tab.id))
  if (!deleted) return
  showDeleteConfirm.value = false
  store.pushNotification({
    type: 'success',
    message: 'Session deleted',
    autoDismissMs: 3000,
  })
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

<style scoped src="@/features/terminal/styles/tab-actions-menu.css"></style>
