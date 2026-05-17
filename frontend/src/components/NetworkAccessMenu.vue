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

  <div
    v-else
    ref="menuRef"
    class="network-access network-access--menu"
  >
    <div class="network-access-menu-heading">
      <div>
        <span>Frontend Access</span>
        <strong>Port {{ frontendPort }}</strong>
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
const menuRef = ref<HTMLElement | null>(null)
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
  if (parentMenuDetails?.open && !networkInfo.value) {
    void fetchNetworkAccess()
  }
}

onMounted(() => {
  document.addEventListener('pointerdown', handleDocumentPointerDown)
  if (props.variant === 'menu') {
    parentMenuDetails = menuRef.value?.closest('details') || null
    if (parentMenuDetails) {
      parentMenuDetails.addEventListener('toggle', handleParentMenuToggle)
      handleParentMenuToggle()
    } else {
      void fetchNetworkAccess()
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
.network-access {
  color: var(--ch-color-text);
}

.network-access--toolbar {
  position: relative;
}

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

.network-access-icon {
  width: 17px;
  height: 17px;
  display: inline-flex;
  align-items: flex-end;
  justify-content: center;
  gap: 2px;
}

.network-access-icon span {
  width: 3px;
  border-radius: 999px;
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

.network-access-panel {
  position: absolute;
  top: calc(100% + 7px);
  right: 0;
  z-index: 60;
  width: min(360px, calc(100vw - 24px));
  padding: 10px;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-glass);
  box-shadow: var(--ch-shadow-lg);
}

.network-access-panel-header,
.network-access-menu-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.network-access-panel-header > div,
.network-access-menu-heading > div {
  min-width: 0;
  display: grid;
  gap: 2px;
}

.network-access-panel-header strong,
.network-access-menu-heading span {
  font-size: 13px;
  font-weight: 800;
  color: var(--ch-color-text);
}

.network-access-panel-header span,
.network-access-menu-heading strong {
  font-size: 11px;
  font-weight: 700;
  color: var(--ch-color-text-muted);
}

.network-access-refresh {
  height: 26px;
  flex: 0 0 auto;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text);
  padding: 0 8px;
  font-size: 11px;
  font-weight: 800;
  cursor: pointer;
}

.network-access-refresh:disabled {
  cursor: default;
  opacity: 0.6;
}

.network-access-port {
  width: fit-content;
  margin-top: 8px;
  padding: 3px 7px;
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-surface-soft);
  color: var(--ch-color-text-muted);
  font-size: 11px;
  font-weight: 800;
}

.network-access-list {
  display: grid;
  gap: 6px;
  margin-top: 8px;
}

.network-access-link {
  min-width: 0;
  display: grid;
  grid-template-columns: minmax(68px, max-content) minmax(0, 1fr) max-content;
  align-items: center;
  gap: 8px;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-surface);
  color: var(--ch-color-text);
  padding: 7px 8px;
  cursor: pointer;
  text-align: left;
}

.network-access-link:hover {
  border-color: var(--ch-color-accent-ring-strong);
  background: var(--ch-color-surface-control-hover);
}

.network-access-link span {
  color: var(--ch-color-text-muted);
  font-size: 11px;
  font-weight: 800;
}

.network-access-link code {
  overflow: hidden;
  color: var(--ch-color-text);
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.network-access-link strong {
  color: var(--ch-color-accent);
  font-size: 11px;
  font-weight: 800;
}

.network-access-status {
  margin: 8px 0 0;
  color: var(--ch-color-text-muted);
  font-size: 11px;
}

.network-access-status--error {
  color: var(--ch-color-danger);
}

.network-access--menu {
  margin-top: 6px;
  padding-top: 8px;
  border-top: 1px solid var(--ch-color-border);
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
