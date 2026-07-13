<template>
  <details
    v-if="props.variant === 'toolbar'"
    ref="detailsRef"
    class="network-access network-access--toolbar"
    @toggle="handleToggle"
  >
    <summary
      class="network-access-trigger"
      title="Frontend access links"
      aria-label="Frontend access links"
    >
      <span
        class="network-access-icon"
        aria-hidden="true"
      >
        <span />
        <span />
        <span />
      </span>
    </summary>
    <div class="network-access-panel">
      <div class="network-access-panel-header">
        <div>
          <strong>Frontend Access</strong>
          <span>{{ networkInfo?.hostname || 'Local machine' }}</span>
        </div>
        <button
          type="button"
          class="network-access-refresh"
          :disabled="isLoading"
          @click="fetchNetworkAccess"
        >
          Refresh
        </button>
      </div>
      <div class="network-access-port">
        Port {{ frontendPort }}
      </div>
      <div class="network-access-list">
        <button
          v-for="link in accessLinks"
          :key="link.host"
          type="button"
          class="network-access-link"
          @click="copyLink(link.url)"
        >
          <span>{{ link.label }}</span>
          <code>{{ link.url }}</code>
          <strong>{{ copiedUrl === link.url ? 'Copied' : 'Copy' }}</strong>
        </button>
      </div>
      <p
        v-if="isLoading"
        class="network-access-status"
      >
        Loading local IPs...
      </p>
      <p
        v-else-if="loadError"
        class="network-access-status network-access-status--error"
      >
        {{ loadError }}
      </p>
      <p
        v-else-if="copyError"
        class="network-access-status network-access-status--error"
      >
        {{ copyError }}
      </p>
    </div>
  </details>

  <details
    v-else
    ref="menuDetailsRef"
    class="network-access network-access--menu"
    @toggle="handleMenuToggle"
  >
    <summary class="network-access-menu-summary">
      <div>
        <span>Frontend Access</span>
        <strong>Port {{ frontendPort }}</strong>
      </div>
      <span
        class="network-access-menu-chevron"
        aria-hidden="true"
      />
    </summary>
    <div class="network-access-submenu-panel">
      <div class="network-access-menu-heading">
        <span>{{ networkInfo?.hostname || 'Local machine' }}</span>
        <button
          type="button"
          class="network-access-refresh"
          :disabled="isLoading"
          @click="fetchNetworkAccess"
        >
          Refresh
        </button>
      </div>
      <div class="network-access-list">
        <button
          v-for="link in accessLinks"
          :key="link.host"
          type="button"
          class="network-access-link"
          @click="copyLink(link.url)"
        >
          <span>{{ link.label }}</span>
          <code>{{ link.url }}</code>
          <strong>{{ copiedUrl === link.url ? 'Copied' : 'Copy' }}</strong>
        </button>
      </div>
      <p
        v-if="isLoading"
        class="network-access-status"
      >
        Loading local IPs...
      </p>
      <p
        v-else-if="loadError"
        class="network-access-status network-access-status--error"
      >
        {{ loadError }}
      </p>
      <p
        v-else-if="copyError"
        class="network-access-status network-access-status--error"
      >
        {{ copyError }}
      </p>
    </div>
  </details>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import type { NetworkAccessInfo } from '@/types'

type NetworkAccessVariant = 'toolbar' | 'menu'

interface AccessLink {
  host: string
  label: string
  url: string
}

const props = withDefaults(defineProps<{
  variant?: NetworkAccessVariant
}>(), {
  variant: 'toolbar',
})

const detailsRef = ref<HTMLDetailsElement | null>(null)
const menuDetailsRef = ref<HTMLDetailsElement | null>(null)
const networkInfo = ref<NetworkAccessInfo | null>(null)
const isLoading = ref(false)
const loadError = ref<string | null>(null)
const copyError = ref<string | null>(null)
const copiedUrl = ref<string | null>(null)
let copyResetTimer: number | null = null
let parentMenuDetails: HTMLDetailsElement | null = null

const frontendProtocol = computed(() =>
  typeof window === 'undefined' ? 'http:' : window.location.protocol
)

const frontendPort = computed(() => {
  if (typeof window === 'undefined') return '5173'
  if (window.location.port) return window.location.port
  return frontendProtocol.value === 'https:' ? '443' : '80'
})

const accessLinks = computed<AccessLink[]>(() => {
  const candidates: { host: string; label: string }[] = [
    { host: '127.0.0.1', label: 'Loopback' },
  ]

  if (typeof window !== 'undefined') {
    const currentHost = normalizeHost(window.location.hostname)
    if (currentHost && currentHost !== 'localhost' && currentHost !== '127.0.0.1') {
      candidates.unshift({ host: currentHost, label: 'Current host' })
    }
  }

  for (const address of networkInfo.value?.addresses || []) {
    candidates.push({ host: address.address, label: address.label })
  }

  const seen = new Set<string>()
  const links: AccessLink[] = []
  for (const candidate of candidates) {
    const key = candidate.host.toLowerCase()
    if (seen.has(key)) continue
    seen.add(key)
    links.push({
      ...candidate,
      url: buildFrontendUrl(candidate.host),
    })
  }
  return links
})

function normalizeHost(host: string): string {
  return host.trim().replace(/^\[/, '').replace(/\]$/, '')
}

function buildFrontendUrl(host: string): string {
  const formattedHost = host.includes(':') ? `[${host}]` : host
  return `${frontendProtocol.value}//${formattedHost}:${frontendPort.value}/`
}

async function readError(response: Response): Promise<string> {
  try {
    const data = await response.json() as { detail?: string }
    return data.detail || response.statusText
  } catch {
    return response.statusText
  }
}

async function fetchNetworkAccess(): Promise<void> {
  if (isLoading.value) return
  isLoading.value = true
  loadError.value = null

  try {
    const response = await fetch('/api/system/network-access')
    if (!response.ok) {
      throw new Error(await readError(response))
    }
    networkInfo.value = await response.json() as NetworkAccessInfo
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : 'Unable to load local IPs'
  } finally {
    isLoading.value = false
  }
}

function handleToggle(event: Event): void {
  const target = event.currentTarget as HTMLDetailsElement
  if (target.open && !networkInfo.value) {
    void fetchNetworkAccess()
  }
}

function handleMenuToggle(event: Event): void {
  const target = event.currentTarget as HTMLDetailsElement
  if (target.open && !networkInfo.value) {
    void fetchNetworkAccess()
  }
}

async function copyLink(url: string): Promise<void> {
  copyError.value = null
  try {
    await writeClipboard(url)
    copiedUrl.value = url
    if (copyResetTimer !== null) {
      window.clearTimeout(copyResetTimer)
    }
    copyResetTimer = window.setTimeout(() => {
      copiedUrl.value = null
      copyResetTimer = null
    }, 1600)
  } catch {
    copyError.value = 'Copy failed'
  }
}

async function writeClipboard(value: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(value)
    return
  }

  const textarea = document.createElement('textarea')
  textarea.value = value
  textarea.setAttribute('readonly', '')
  textarea.style.position = 'fixed'
  textarea.style.top = '-999px'
  document.body.appendChild(textarea)
  textarea.select()
  const didCopy = document.execCommand('copy')
  document.body.removeChild(textarea)
  if (!didCopy) {
    throw new Error('copy failed')
  }
}

function handleDocumentPointerDown(event: PointerEvent): void {
  if (!detailsRef.value?.open) return
  const target = event.target as Node | null
  if (target && !detailsRef.value.contains(target)) {
    detailsRef.value.open = false
  }
}

function handleParentMenuToggle(): void {
  if (!parentMenuDetails?.open && menuDetailsRef.value?.open) {
    menuDetailsRef.value.open = false
  }
}

onMounted(() => {
  document.addEventListener('pointerdown', handleDocumentPointerDown)
  if (props.variant === 'menu') {
    parentMenuDetails = menuDetailsRef.value?.parentElement?.closest('details') || null
    if (parentMenuDetails) {
      parentMenuDetails.addEventListener('toggle', handleParentMenuToggle)
    }
  }
})

onUnmounted(() => {
  document.removeEventListener('pointerdown', handleDocumentPointerDown)
  parentMenuDetails?.removeEventListener('toggle', handleParentMenuToggle)
  if (copyResetTimer !== null) {
    window.clearTimeout(copyResetTimer)
  }
})
</script>

<style scoped>
/*
 * Network access menu — toolbar signal/wifi trigger and menu-item submenu.
 *
 * Styling consumes the global design-token scale defined in App.vue :root
 * (--ch-space-*, --ch-font-*, --ch-leading-*, --ch-weight-*) plus the
 * established color/radius/shadow/motion tokens. Hardcoded px values
 * remain only for functional constants: the 32px toolbar trigger size
 * (toolbar-icon convention), 1px borders, signal-bar stroke width,
 * signal-bar proportional heights (6/11/16px — decorative glyph whose
 * shape must stay intact), and 360px panel max-width. Per reviewer
 * note, --ch-shadow-lg is not defined; the popover shadow uses
 * --ch-shadow-popover to match other floating panels.
 */

.network-access {
  color: var(--ch-color-text);
}

.network-access--toolbar {
  position: relative;
}

/* --- Toolbar trigger (signal/wifi glyph) -------------------------------- */

.network-access-trigger {
  width: 32px;
  height: 32px;
  min-height: 32px;
  display: grid;
  place-items: center;
  flex: 0 0 32px;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-sunken);
  color: var(--ch-color-text-muted);
  cursor: pointer;
  list-style: none;
  line-height: 1;
  padding: 0;
  appearance: none;
  transition: background var(--ch-motion-fast), border-color var(--ch-motion-fast), color var(--ch-motion-fast);
}

.network-access-trigger::-webkit-details-marker {
  display: none;
}

.network-access-trigger:hover,
.network-access--toolbar[open] .network-access-trigger {
  background: var(--ch-color-surface-control-hover);
  border-color: var(--ch-color-accent-ring-strong);
  color: var(--ch-color-text);
}

/* Signal-bars glyph: 16x16 flex-end box (--ch-space-4) with three bars of
 * proportional height (6/11/16px) so the tallest bar fills the box. Bar
 * width stays 3px (glyph stroke), inter-bar gap uses space-1. */
.network-access-icon {
  width: var(--ch-space-4);
  height: var(--ch-space-4);
  display: inline-flex;
  align-items: flex-end;
  justify-content: center;
  gap: var(--ch-space-1);
}

.network-access-icon span {
  width: 3px;
  border-radius: var(--ch-radius-pill);
  background: currentColor;
}

.network-access-icon span:nth-child(1) {
  height: 6px;
}

.network-access-icon span:nth-child(2) {
  height: 11px;
}

.network-access-icon span:nth-child(3) {
  height: 16px;
}

/* --- Toolbar popover panel --------------------------------------------- */

.network-access-panel {
  position: absolute;
  top: calc(100% + var(--ch-space-2));
  right: 0;
  z-index: 1200;
  width: min(360px, calc(100vw - 24px));
  padding: var(--ch-space-2);
  border: 1px solid var(--ch-color-border-strong);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-glass);
  box-shadow: var(--ch-shadow-popover);
  animation: network-access-panel-in var(--ch-motion-fast) var(--ch-motion-ease);
  transform-origin: top right;
}

@keyframes network-access-panel-in {
  from {
    opacity: 0;
    transform: translateY(-4px) scale(0.98);
  }
  to {
    opacity: 1;
    transform: translateY(0) scale(1);
  }
}

.network-access-panel-header,
.network-access-menu-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--ch-space-3);
}

.network-access-panel-header > div,
.network-access-menu-heading > div {
  min-width: 0;
  display: grid;
  gap: var(--ch-space-1);
}

.network-access-panel-header strong,
.network-access-menu-heading span {
  font-size: var(--ch-font-md);
  font-weight: var(--ch-weight-semibold);
  line-height: var(--ch-leading-tight);
  color: var(--ch-color-text);
}

.network-access-panel-header span,
.network-access-menu-heading strong {
  font-size: var(--ch-font-xs);
  font-weight: var(--ch-weight-medium);
  line-height: var(--ch-leading-tight);
  color: var(--ch-color-text-muted);
}

.network-access-refresh {
  height: 28px;
  flex: 0 0 auto;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text);
  padding: 0 var(--ch-space-2);
  font-size: var(--ch-font-xs);
  font-weight: var(--ch-weight-medium);
  line-height: var(--ch-leading-tight);
  cursor: pointer;
}

.network-access-refresh:disabled {
  cursor: default;
  opacity: 0.6;
}

.network-access-port {
  width: fit-content;
  margin-top: var(--ch-space-2);
  padding: 2px var(--ch-space-2);
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-surface-soft);
  color: var(--ch-color-text-muted);
  font-size: var(--ch-font-xs);
  font-weight: var(--ch-weight-medium);
  line-height: var(--ch-leading-tight);
}

.network-access-list {
  display: grid;
  gap: var(--ch-space-2);
  margin-top: var(--ch-space-2);
}

.network-access-link {
  min-width: 0;
  display: grid;
  grid-template-columns: minmax(68px, max-content) minmax(0, 1fr) max-content;
  align-items: center;
  gap: var(--ch-space-2);
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-surface);
  color: var(--ch-color-text);
  padding: var(--ch-space-2);
  cursor: pointer;
  text-align: left;
  transition: background var(--ch-motion-fast), border-color var(--ch-motion-fast);
}

.network-access-link:hover {
  border-color: var(--ch-color-accent-ring-strong);
  background: var(--ch-color-surface-control-hover);
}

.network-access-link span {
  color: var(--ch-color-text-muted);
  font-size: var(--ch-font-xs);
  font-weight: var(--ch-weight-medium);
  line-height: var(--ch-leading-tight);
}

.network-access-link code {
  overflow: hidden;
  color: var(--ch-color-text);
  font-size: var(--ch-font-sm);
  line-height: var(--ch-leading-tight);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.network-access-link strong {
  color: var(--ch-color-accent);
  font-size: var(--ch-font-xs);
  font-weight: var(--ch-weight-medium);
  line-height: var(--ch-leading-tight);
}

.network-access-status {
  margin: var(--ch-space-2) 0 0;
  color: var(--ch-color-text-muted);
  font-size: var(--ch-font-xs);
  line-height: var(--ch-leading-normal);
}

.network-access-status--error {
  color: var(--ch-color-danger);
}

/* --- Menu-item variant (embedded in a parent <menu>) ------------------- */

.network-access--menu {
  margin-top: var(--ch-space-1);
  padding-top: var(--ch-space-1);
  border-top: 1px solid var(--ch-color-border);
}

.network-access--menu summary {
  list-style: none;
}

.network-access--menu summary::-webkit-details-marker {
  display: none;
}

.network-access-menu-summary {
  width: 100%;
  min-height: 34px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--ch-space-2);
  border: 1px solid transparent;
  border-radius: var(--ch-radius-sm);
  background: transparent;
  color: var(--ch-color-text);
  padding: var(--ch-space-1) var(--ch-space-2);
  cursor: pointer;
  transition: background var(--ch-motion-fast);
}

.network-access-menu-summary:hover,
.network-access--menu[open] > .network-access-menu-summary {
  background: var(--ch-color-surface-control-hover);
}

.network-access-menu-summary:focus-visible {
  outline: 2px solid var(--ch-color-accent-ring-strong);
  outline-offset: 2px;
}

.network-access-menu-summary > div {
  min-width: 0;
  display: grid;
  gap: 0;
}

.network-access-menu-summary span {
  overflow: hidden;
  color: var(--ch-color-text);
  font-size: var(--ch-font-sm);
  font-weight: var(--ch-weight-semibold);
  line-height: var(--ch-leading-tight);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.network-access-menu-summary strong {
  color: var(--ch-color-text-muted);
  font-size: var(--ch-font-xs);
  font-weight: var(--ch-weight-medium);
  line-height: var(--ch-leading-tight);
  text-transform: uppercase;
}

.network-access-menu-chevron {
  width: var(--ch-space-2);
  height: var(--ch-space-2);
  flex: 0 0 auto;
  border-right: 2px solid currentColor;
  border-bottom: 2px solid currentColor;
  color: var(--ch-color-text-muted);
  transform: rotate(-45deg);
  transition: transform var(--ch-motion-fast);
}

.network-access--menu[open] .network-access-menu-chevron {
  transform: rotate(45deg);
}

.network-access-submenu-panel {
  padding: var(--ch-space-1) 0 0;
}

.network-access--menu .network-access-menu-heading {
  padding: 0;
}

.network-access--menu .network-access-link {
  grid-template-columns: minmax(0, 1fr) max-content;
  grid-template-areas:
    "label action"
    "url url";
}

.network-access--menu .network-access-link span {
  grid-area: label;
}

.network-access--menu .network-access-link code {
  grid-area: url;
}

.network-access--menu .network-access-link strong {
  grid-area: action;
}
</style>
