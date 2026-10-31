<template>
  <div
    ref="rootRef"
    class="quota-menu"
  >
    <button
      ref="triggerRef"
      type="button"
      class="quota-menu-summary"
      title="Provider quota"
      aria-label="Provider quota"
      aria-haspopup="dialog"
      :aria-expanded="isOpen"
      @click="openModal"
    >
      <svg
        class="quota-menu-icon"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="1.8"
        stroke-linecap="round"
        stroke-linejoin="round"
        aria-hidden="true"
        focusable="false"
      >
        <path d="M4 17a8 8 0 1 1 16 0" />
        <path d="M12 17l4.5-4.5" />
      </svg>
      <div>
        <span>Provider quota</span>
        <strong>{{ summaryText }}</strong>
      </div>
    </button>

    <Teleport
      :to="teleportTarget"
      :disabled="!teleportTarget"
    >
      <div
        v-if="isOpen"
        class="quota-modal-backdrop"
        @click.self="closeModal"
        @keydown.esc.stop.prevent="closeModal"
        @keydown.tab="trapFocus"
      >
        <div
          ref="modalRef"
          class="quota-modal"
          role="dialog"
          aria-modal="true"
          aria-labelledby="quota-modal-title"
          tabindex="-1"
        >
          <div class="quota-modal-header">
            <div class="quota-modal-title-group">
              <h3 id="quota-modal-title">
                Provider quota
              </h3>
              <span>Codex, MASO, and Antigravity</span>
            </div>
            <div class="quota-modal-actions">
              <button
                type="button"
                class="quota-refresh"
                :disabled="isLoading"
                aria-label="Refresh provider quota"
                @click="refreshQuota"
              >
                Refresh
              </button>
              <button
                ref="closeButtonRef"
                type="button"
                class="quota-modal-close"
                aria-label="Close provider quota"
                @click="closeModal"
              >
                Close
              </button>
            </div>
          </div>

          <div class="quota-modal-body">
            <section
              v-for="view in providerViews"
              :key="view.key"
              class="quota-provider"
            >
              <div class="quota-provider-head">
                <strong>{{ view.title }}</strong>
                <span
                  class="quota-badge"
                  :class="`quota-badge--${view.state}`"
                >{{ view.statusText }}</span>
              </div>
              <p
                v-if="view.asOfText"
                class="quota-reading"
                :class="{ 'quota-reading--aging': view.aging }"
                :title="view.asOfTitle"
              >
                {{ view.asOfText }}
              </p>
              <p
                v-if="view.staleNote"
                class="quota-note quota-note--warning"
              >
                {{ view.staleNote }}
              </p>
              <p
                v-if="view.agingNote"
                class="quota-note quota-note--aging"
              >
                {{ view.agingNote }}
              </p>
              <p
                v-if="view.unchangedNote"
                class="quota-note quota-note--unchanged"
                role="status"
              >
                {{ view.unchangedNote }}
              </p>
              <p
                v-if="view.message"
                class="quota-note"
                :class="`quota-note--${view.messageTone}`"
              >
                {{ view.message }}
              </p>
              <ul
                v-if="view.rows.length"
                class="quota-rows"
              >
                <li
                  v-for="row in view.rows"
                  :key="row.key"
                  class="quota-row"
                  :class="[`quota-row--${row.state}`, `quota-row--${row.tone}`]"
                >
                  <div class="quota-row-head">
                    <span
                      class="quota-row-label"
                      :title="row.label"
                    >{{ row.label }}</span>
                    <strong class="quota-row-value">{{ row.value }}</strong>
                  </div>
                  <div
                    v-if="row.barPercent !== null"
                    class="quota-bar"
                    aria-hidden="true"
                  >
                    <span :style="{ width: `${row.barPercent}%` }" />
                  </div>
                  <p
                    v-if="row.usedText || row.resetText"
                    class="quota-row-meta"
                  >
                    <span v-if="row.usedText">{{ row.usedText }}</span>
                    <span
                      v-if="row.resetText"
                      :title="row.resetTitle"
                    >{{ row.resetText }}</span>
                  </p>
                </li>
              </ul>
              <p
                v-else-if="view.emptyText"
                class="quota-status"
              >
                {{ view.emptyText }}
              </p>
              <p
                v-for="note in view.notes"
                :key="note"
                class="quota-note"
              >
                {{ note }}
              </p>
            </section>

            <p
              v-if="isLoading"
              class="quota-status"
            >
              Loading quota...
            </p>
            <div
              v-else-if="loadError"
              class="quota-failure"
              role="alert"
            >
              <p class="quota-status quota-status--error">
                {{ loadError }}
              </p>
              <button
                type="button"
                class="quota-refresh"
                @click="retryQuota"
              >
                Retry
              </button>
            </div>
            <p
              v-else-if="!providerViews.length"
              class="quota-status"
            >
              No provider reported quota.
            </p>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import type { QuotaResponse } from '@/types'
import {
  buildQuotaView,
  findUnchangedProviders,
  quotaRequestUrl,
  summarizeQuota,
} from '@/utils/quotaDisplay'

const emit = defineEmits<{ select: [] }>()

// Readings age while the modal stays open, so recompute the "updated x ago"
// text on a slow tick instead of freezing it at load time.
const AGE_TICK_MS = 30_000
const teleportTarget = typeof document !== 'undefined' ? document.body : null

const rootRef = ref<HTMLElement | null>(null)
const triggerRef = ref<HTMLElement | null>(null)
const modalRef = ref<HTMLElement | null>(null)
const closeButtonRef = ref<HTMLElement | null>(null)
const isOpen = ref(false)
const quota = ref<QuotaResponse | null>(null)
const isLoading = ref(false)
const loadError = ref<string | null>(null)
const now = ref(Date.now())
// Providers that answered the last refresh with the same reading. Recomputed
// on every successful load, so it clears as soon as one really moves.
const unchangedSinceRefresh = ref<string[]>([])

let requestSeq = 0
let inFlight: AbortController | null = null
// Retry must repeat the request that failed, not silently change it.
let lastRefreshRequested = false
let ageTimer: ReturnType<typeof setInterval> | null = null
let returnFocusEl: HTMLElement | null = null

const providerViews = computed(() => buildQuotaView(quota.value, now.value, {
  unchangedProviders: unchangedSinceRefresh.value,
}))

const summaryText = computed(() => {
  if (!quota.value) {
    if (isLoading.value) return 'Loading'
    return loadError.value ? 'Load failed' : 'Not loaded'
  }
  return summarizeQuota(providerViews.value)
})

async function readError(response: Response): Promise<string> {
  try {
    const data = await response.json() as { detail?: string }
    return data.detail || response.statusText
  } catch {
    return response.statusText
  }
}

async function fetchQuota(refresh: boolean): Promise<void> {
  lastRefreshRequested = refresh
  inFlight?.abort()
  const controller = new AbortController()
  const seq = ++requestSeq
  inFlight = controller
  isLoading.value = true
  loadError.value = null

  try {
    const response = await fetch(quotaRequestUrl(refresh), { signal: controller.signal })
    if (!response.ok) {
      throw new Error(await readError(response))
    }
    const payload = await response.json() as QuotaResponse
    // A superseded or unmounted request must never overwrite the modal.
    if (seq !== requestSeq) return
    // Compare against the reading this request replaces, so a refresh that
    // changed nothing can say so instead of looking like a dead button.
    unchangedSinceRefresh.value = findUnchangedProviders(quota.value, payload)
    quota.value = payload
    now.value = Date.now()
  } catch (error) {
    if (seq !== requestSeq) return
    const reason = error instanceof Error ? error.message : ''
    loadError.value = reason ? `Could not load quota: ${reason}` : 'Could not load quota.'
  } finally {
    if (seq === requestSeq) {
      isLoading.value = false
      inFlight = null
    }
  }
}

function refreshQuota(): void {
  if (isLoading.value) return
  void fetchQuota(true)
}

function retryQuota(): void {
  if (isLoading.value) return
  void fetchQuota(lastRefreshRequested)
}

function startAgeTicker(): void {
  if (ageTimer !== null) return
  ageTimer = setInterval(() => { now.value = Date.now() }, AGE_TICK_MS)
}

function stopAgeTicker(): void {
  if (ageTimer === null) return
  clearInterval(ageTimer)
  ageTimer = null
}

function openModal(): void {
  const parentMenu = rootRef.value?.closest?.('details') as HTMLDetailsElement | null
  const parentSummary = parentMenu?.querySelector?.('summary') as HTMLElement | null
  isOpen.value = true
  now.value = Date.now()
  startAgeTicker()
  if (parentMenu) parentMenu.open = false
  emit('select')
  const activeEl = typeof document !== 'undefined' && document.activeElement instanceof HTMLElement
    ? document.activeElement
    : null
  returnFocusEl = parentSummary ?? activeEl ?? triggerRef.value
  void nextTick(() => {
    if (typeof closeButtonRef.value?.focus === 'function') {
      closeButtonRef.value.focus()
    } else if (typeof modalRef.value?.focus === 'function') {
      modalRef.value.focus()
    }
  })
  if (!quota.value && !isLoading.value) {
    void fetchQuota(false)
  }
}

function closeModal(): void {
  if (!isOpen.value) return
  isOpen.value = false
  stopAgeTicker()
  const target = returnFocusEl
  returnFocusEl = null
  if (target && typeof target.focus === 'function') {
    void nextTick(() => target.focus())
  }
}

function trapFocus(event: KeyboardEvent): void {
  if (event.defaultPrevented) return
  const dialog = modalRef.value
  if (!dialog || typeof dialog.querySelectorAll !== 'function') return
  const focusable = [
    ...dialog.querySelectorAll<HTMLElement>(
      'button:not(:disabled), [href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])',
    ),
  ]
  const first = focusable[0]
  const last = focusable[focusable.length - 1]
  if (!first || !last) {
    event.preventDefault()
    dialog.focus?.()
    return
  }
  const active = typeof document !== 'undefined' ? (document.activeElement as HTMLElement | null) : null
  if (!active || !focusable.includes(active)) {
    event.preventDefault()
    ;(event.shiftKey ? last : first).focus?.()
  } else if (event.shiftKey && active === first) {
    event.preventDefault()
    last.focus?.()
  } else if (!event.shiftKey && active === last) {
    event.preventDefault()
    first.focus?.()
  }
}

function handleKeydown(event: KeyboardEvent): void {
  if (!isOpen.value) return
  if (event.key === 'Escape') {
    event.preventDefault()
    event.stopPropagation()
    closeModal()
  } else if (event.key === 'Tab') {
    trapFocus(event)
  }
}

onMounted(() => {
  if (typeof window !== 'undefined') {
    window.addEventListener('keydown', handleKeydown, true)
  }
})

onUnmounted(() => {
  if (typeof window !== 'undefined') {
    window.removeEventListener('keydown', handleKeydown, true)
  }
  stopAgeTicker()
  // Retire anything in flight so a late response cannot touch a dead modal.
  requestSeq += 1
  inFlight?.abort()
  inFlight = null
})
</script>

<style scoped>
.quota-menu {
  margin-top: 4px;
  padding-top: 4px;
  border-top: 1px solid var(--ch-color-border);
  color: var(--ch-color-text);
}

.quota-menu-summary {
  width: 100%;
  min-height: 36px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  border: 0;
  border-radius: var(--ch-radius-md);
  background: transparent;
  color: var(--ch-color-text);
  font: inherit;
  text-align: left;
  padding: 8px 10px;
  cursor: pointer;
  transition: var(--ch-motion-fast);
}

.quota-menu-summary:hover {
  background: var(--ch-color-surface-control-hover);
}

.quota-menu-summary:focus-visible {
  outline: none;
  box-shadow: 0 0 0 3px var(--ch-color-accent-ring);
}

.quota-menu-summary > div {
  flex: 1;
  min-width: 0;
  display: grid;
  gap: 1px;
}

.quota-menu-icon {
  width: 18px;
  height: 18px;
  flex: 0 0 18px;
  color: var(--ch-color-text-muted);
}

.quota-menu-summary span {
  overflow: hidden;
  color: var(--ch-color-text);
  font-size: var(--ch-font-size-sm);
  font-weight: 500;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.quota-menu-summary strong {
  /* The headline can carry a warning ("1 provider unavailable", "stale"), so
     it wraps instead of truncating it away in a narrow menu. */
  color: var(--ch-color-text-muted);
  font-size: var(--ch-font-size-xs);
  font-weight: 600;
  line-height: 1.35;
  overflow-wrap: anywhere;
}

.quota-modal-backdrop {
  position: fixed;
  inset: 0;
  z-index: 1000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
  background: rgba(15, 23, 42, 0.48);
  backdrop-filter: blur(2px);
}

.quota-modal {
  width: min(560px, 100%);
  max-height: min(82dvh, 720px);
  display: flex;
  flex-direction: column;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-lg);
  background: var(--ch-color-surface);
  color: var(--ch-color-text);
  box-shadow: var(--ch-shadow-popover);
  overflow: hidden;
}

.quota-modal-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 14px 16px;
  border-bottom: 1px solid var(--ch-color-border);
}

.quota-modal-title-group {
  min-width: 0;
  display: grid;
  gap: 2px;
}

.quota-modal-title-group h3 {
  margin: 0;
  color: var(--ch-color-text);
  font-size: var(--ch-font-size-md, 15px);
  font-weight: 600;
  line-height: 1.3;
}

.quota-modal-title-group span {
  color: var(--ch-color-text-muted);
  font-size: var(--ch-font-size-xs);
}

.quota-modal-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 0 0 auto;
}

.quota-modal-body {
  padding: 14px 16px 16px;
  overflow-y: auto;
  display: grid;
  gap: 10px;
}

.quota-refresh,
.quota-modal-close {
  height: 30px;
  flex: 0 0 auto;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text);
  padding: 0 12px;
  font-size: var(--ch-font-size-xs);
  font-weight: 600;
  cursor: pointer;
  transition: var(--ch-motion-fast);
}

.quota-refresh:hover,
.quota-modal-close:hover {
  border-color: var(--ch-color-accent-ring-strong);
  background: var(--ch-color-surface-control-hover);
}

.quota-refresh:focus-visible,
.quota-modal-close:focus-visible {
  outline: none;
  box-shadow: 0 0 0 3px var(--ch-color-accent-ring);
}

.quota-refresh:disabled {
  cursor: default;
  opacity: 0.6;
}

.quota-provider {
  padding: 10px 12px;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface);
}

.quota-provider-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.quota-provider-head strong {
  min-width: 0;
  overflow: hidden;
  color: var(--ch-color-text);
  font-size: var(--ch-font-size-sm);
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.quota-badge {
  flex: 0 0 auto;
  padding: 2px 7px;
  border-radius: 999px;
  background: var(--ch-color-surface-soft);
  color: var(--ch-color-text-muted);
  font-size: var(--ch-font-size-xs);
  font-weight: 600;
}

.quota-badge--within {
  background: var(--ch-color-success-bg);
  color: var(--ch-color-success-strong);
}

.quota-badge--near {
  background: var(--ch-color-warning-bg);
  color: var(--ch-color-warning-strong);
}

.quota-badge--over {
  background: var(--ch-color-danger-bg);
  color: var(--ch-color-danger-strong);
}

.quota-badge--unavailable {
  background: var(--ch-color-attention-bg);
  color: var(--ch-color-attention-strong);
}

/* Not a failure, so it must not look like one: no fill, no alarm color. */
.quota-badge--not_configured {
  border: 1px dashed var(--ch-color-border-strong);
  background: transparent;
  color: var(--ch-color-text-subtle);
  font-weight: 500;
}

.quota-reading {
  margin: 4px 0 0;
  color: var(--ch-color-text-muted);
  font-size: var(--ch-font-size-xs);
}

/* An old reading must not look like a current one at a glance. */
.quota-reading--aging {
  color: var(--ch-color-attention-strong);
  font-weight: 700;
}

.quota-note {
  margin: 4px 0 0;
  color: var(--ch-color-text-muted);
  font-size: var(--ch-font-size-xs);
  line-height: 1.4;
}

.quota-note--warning {
  color: var(--ch-color-warning-strong);
  font-weight: 600;
}

.quota-note--aging {
  color: var(--ch-color-attention-strong);
}

.quota-note--unchanged {
  padding: 6px 8px;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-surface-soft);
  color: var(--ch-color-text);
}

.quota-note--error {
  color: var(--ch-color-danger);
}

/* A neutral statement about this deployment, not a problem to act on. */
.quota-note--neutral {
  color: var(--ch-color-text-muted);
}

.quota-rows {
  display: grid;
  gap: 6px;
  margin: 8px 0 0;
  padding: 0;
  list-style: none;
}

.quota-row {
  --quota-fill: var(--ch-color-border-strong);
  --quota-value: var(--ch-color-text-muted);

  min-width: 0;
  display: grid;
  gap: 4px;
  padding: 6px 8px;
  border-inline-start: 2px solid var(--quota-fill);
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-surface-soft);
}

.quota-row--healthy {
  --quota-fill: var(--ch-color-success);
  --quota-value: var(--ch-color-success-strong);
}

.quota-row--caution {
  --quota-fill: var(--ch-color-warning);
  --quota-value: var(--ch-color-warning-strong);
}

.quota-row--low {
  --quota-fill: color-mix(in srgb, var(--ch-color-warning) 45%, var(--ch-color-danger));
  --quota-value: color-mix(in srgb, var(--ch-color-warning-strong) 45%, var(--ch-color-danger));
}

.quota-row--critical {
  --quota-fill: var(--ch-color-danger);
  --quota-value: var(--ch-color-danger-text);
}

.quota-row-head {
  min-width: 0;
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
}

.quota-row-label {
  min-width: 0;
  overflow: hidden;
  color: var(--ch-color-text-muted);
  font-size: var(--ch-font-size-xs);
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.quota-row-value {
  flex: 0 0 auto;
  color: var(--quota-value);
  font-size: var(--ch-font-size-sm);
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}

.quota-row--no_data .quota-row-value,
.quota-row--unknown .quota-row-value,
.quota-row--no_limit .quota-row-value {
  color: var(--ch-color-text-muted);
  font-size: var(--ch-font-size-xs);
  font-weight: 600;
}

.quota-bar {
  height: 5px;
  overflow: hidden;
  border-radius: 999px;
  background: var(--ch-color-border);
}

.quota-bar span {
  display: block;
  height: 100%;
  border-radius: 999px;
  background: var(--quota-fill);
}

.quota-row-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 2px 8px;
  margin: 0;
  color: var(--ch-color-text-muted);
  font-size: var(--ch-font-size-xs);
  font-variant-numeric: tabular-nums;
}

.quota-status {
  margin: 8px 0 0;
  color: var(--ch-color-text-muted);
  font-size: var(--ch-font-size-xs);
}

.quota-status--error {
  color: var(--ch-color-danger);
}

.quota-failure {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-top: 4px;
}

.quota-failure .quota-status {
  min-width: 0;
  margin: 0;
}
</style>
