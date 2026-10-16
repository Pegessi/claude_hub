<template>
  <details
    ref="detailsRef"
    class="quota-menu"
    @toggle="handleToggle"
  >
    <summary
      class="quota-menu-summary"
      title="Provider quota"
      aria-label="Provider quota"
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
      <span
        class="quota-menu-chevron"
        aria-hidden="true"
      />
    </summary>
    <div class="quota-submenu-panel">
      <div class="quota-menu-heading">
        <span>Codex and MASO</span>
        <button
          type="button"
          class="quota-refresh"
          :disabled="isLoading"
          aria-label="Refresh provider quota"
          @click="refreshQuota"
        >
          Refresh
        </button>
      </div>

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
  </details>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import type { QuotaResponse } from '@/types'
import {
  buildQuotaView,
  findUnchangedProviders,
  quotaRequestUrl,
  summarizeQuota,
} from '@/utils/quotaDisplay'

// Readings age while the panel stays open, so recompute the "updated x ago"
// text on a slow tick instead of freezing it at load time.
const AGE_TICK_MS = 30_000

const detailsRef = ref<HTMLDetailsElement | null>(null)
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
let parentMenuDetails: HTMLDetailsElement | null = null

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
    // A superseded or unmounted request must never overwrite the panel.
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

function handleToggle(event: Event): void {
  const target = event.currentTarget as HTMLDetailsElement
  if (!target.open) {
    stopAgeTicker()
    return
  }
  now.value = Date.now()
  startAgeTicker()
  if (!quota.value && !isLoading.value) {
    void fetchQuota(false)
  }
}

function handleParentMenuToggle(): void {
  if (!parentMenuDetails?.open && detailsRef.value?.open) {
    detailsRef.value.open = false
  }
}

onMounted(() => {
  parentMenuDetails = detailsRef.value?.parentElement?.closest('details') || null
  parentMenuDetails?.addEventListener('toggle', handleParentMenuToggle)
})

onUnmounted(() => {
  parentMenuDetails?.removeEventListener('toggle', handleParentMenuToggle)
  stopAgeTicker()
  // Retire anything in flight so a late response cannot touch a dead panel.
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

.quota-menu summary {
  list-style: none;
}

.quota-menu summary::-webkit-details-marker {
  display: none;
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
  padding: 8px 10px;
  cursor: pointer;
  transition: var(--ch-motion-fast);
}

.quota-menu-summary:hover,
.quota-menu[open] > .quota-menu-summary {
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

.quota-menu-chevron {
  width: 7px;
  height: 7px;
  flex: 0 0 auto;
  border-right: 2px solid currentColor;
  border-bottom: 2px solid currentColor;
  color: var(--ch-color-text-muted);
  transform: rotate(-45deg);
  transition: transform var(--ch-motion-fast);
}

.quota-menu[open] .quota-menu-chevron {
  transform: rotate(45deg);
}

.quota-submenu-panel {
  padding: 4px 2px 2px;
}

.quota-menu-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.quota-menu-heading > span {
  min-width: 0;
  overflow: hidden;
  color: var(--ch-color-text);
  font-size: var(--ch-font-size-sm);
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.quota-refresh {
  height: 28px;
  flex: 0 0 auto;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text);
  padding: 0 10px;
  font-size: var(--ch-font-size-xs);
  font-weight: 600;
  cursor: pointer;
  transition: var(--ch-motion-fast);
}

.quota-refresh:hover {
  border-color: var(--ch-color-accent-ring-strong);
  background: var(--ch-color-surface-control-hover);
}

.quota-refresh:focus-visible {
  outline: none;
  box-shadow: 0 0 0 3px var(--ch-color-accent-ring);
}

.quota-refresh:disabled {
  cursor: default;
  opacity: 0.6;
}

.quota-provider {
  margin-top: 8px;
  padding: 8px;
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
