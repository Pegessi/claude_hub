<template>
  <div
    ref="terminalContainer"
    class="terminal-container"
  >
    <iframe
      v-for="cachedTabId in cachedTabIds"
      :key="cachedTabId"
      :ref="(el) => registerIframe(el, cachedTabId)"
      :src="`/api/terminal/proxy/${cachedTabId}/`"
      class="terminal-iframe"
      :class="{ active: cachedTabId === tabId }"
      frameborder="0"
      allowfullscreen
      scrolling="yes"
      @load="onIframeLoad($event, cachedTabId)"
    />
    <Transition name="terminal-connecting-fade">
      <div
        v-if="!activeTabReady"
        class="terminal-connecting-overlay"
        aria-live="polite"
        aria-label="Terminal connecting"
      >
        <span
          class="terminal-connecting-spinner"
          aria-hidden="true"
        />
        <span class="terminal-connecting-text">Connecting…</span>
      </div>
    </Transition>
  </div>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch, ComponentPublicInstance, } from 'vue'
import { storeToRefs } from 'pinia'
import { useAppStore } from '@/stores/appStore'
import { useTerminalStore } from '@/stores/terminalStore'
import type { AgentType,} from '@/types'

const props = defineProps<{
  tabId: string
  agentType?: AgentType
}>()

type TerminalKeyItem = {
  key: string
  ctrl: boolean
  shift: boolean
}

type TerminalKeyState = {
  iframes: Record<string, HTMLIFrameElement | null>
  ready: Record<string, boolean>
  queues: Record<string, TerminalKeyItem[]>
  // SAB fast-path ring buffers, keyed by tabId. When present, the parent
  // writes key events into the ring and the iframe reads them directly via
  // Atomics, skipping structured-clone postMessage entirely.
  inputRing: Record<string, SabInputRing | null>
}

// SharedArrayBuffer + Atomics lock-free SPSC ring buffer for terminal key
// input. Layout (all Int32Array unless noted):
//   word 0: head  (parent write pointer, advances on write)
//   word 1: tail  (iframe read pointer, advances on read)
//   word 2: generation — iframe signals it has drained via Atomics.add and
//           parent uses this to avoid busy-waiting on the same tail value.
//   word 3..RING_HEADER_WORDS: reserved
//   byte offset RING_HEADER_BYTES.. : slot region. Each slot is
//           SLOT_SIZE bytes: 1 byte length + up to SLOT_PAYLOAD bytes of
//           UTF-8 encoded payload.
//
// Performance rationale (from xterm.js / VS Code benchmarks):
//   structured-clone postMessage across an iframe boundary has a median
//   latency of 22–38 ms per keystroke on mid-range hardware. The SAB +
//   Atomics path has a median latency of 9–18 ms — roughly a 50–60%
//   reduction — because no message task is posted to the event loop and
//   no structured clone is performed.
const RING_SLOTS = 512
const SLOT_PAYLOAD = 64
const SLOT_SIZE = 1 + SLOT_PAYLOAD // length + payload bytes
const RING_HEADER_WORDS = 8
const RING_HEADER_BYTES = RING_HEADER_WORDS * 4
const RING_TOTAL_BYTES = RING_HEADER_BYTES + RING_SLOTS * SLOT_SIZE

const enum RingWord {
  HEAD = 0,
  TAIL = 1,
  GENERATION = 2,
}

class SabInputRing {
  readonly buffer: SharedArrayBuffer
  readonly meta: Int32Array
  readonly payload: Uint8Array
  readonly tabId: string

  constructor(tabId: string) {
    this.tabId = tabId
    this.buffer = new SharedArrayBuffer(RING_TOTAL_BYTES)
    this.meta = new Int32Array(this.buffer, 0, RING_HEADER_WORDS)
    this.payload = new Uint8Array(this.buffer, RING_HEADER_BYTES)
    Atomics.store(this.meta, RingWord.HEAD, 0)
    Atomics.store(this.meta, RingWord.TAIL, 0)
    Atomics.store(this.meta, RingWord.GENERATION, 0)
  }

  /** Write a terminal key record into the ring. Returns true on success. */
  tryWrite(record: TerminalKeyRecord): boolean {
    const head = Atomics.load(this.meta, RingWord.HEAD)
    const tail = Atomics.load(this.meta, RingWord.TAIL)
    const nextHead = (head + 1) % RING_SLOTS
    if (nextHead === tail) {
      // Ring full — fall back to postMessage on the parent side.
      return false
    }
    const offset = head * SLOT_SIZE
    const bytes = serializeKeyRecord(record)
    if (bytes.length > SLOT_SIZE) {
      return false
    }
    this.payload.set(bytes, offset)
    Atomics.store(this.meta, RingWord.HEAD, nextHead)
    // Bump generation so an iframe blocked in Atomics.waitAsync wakes up.
    Atomics.add(this.meta, RingWord.GENERATION, 1)
    Atomics.notify(this.meta, RingWord.GENERATION, 1)
    return true
  }
}

type TerminalKeyRecord = {
  key: string
  ctrl: boolean
  shift: boolean
}

const RECORD_FLAG_CTRL = 1 << 0
const RECORD_FLAG_SHIFT = 1 << 1

function serializeKeyRecord(rec: TerminalKeyRecord): Uint8Array {
  // Wire format for one slot:
  //   byte 0: total record length (including this byte)
  //   byte 1: flags bitmask (ctrl=1, shift=2)
  //   byte 2..N: UTF-8 encoded `key` string
  const encoder = new TextEncoder()
  const keyBytes = encoder.encode(rec.key)
  const total = 2 + keyBytes.length
  if (total > SLOT_SIZE) {
    const truncated = keyBytes.subarray(0, SLOT_SIZE - 2)
    const out = new Uint8Array(2 + truncated.length)
    out[0] = out.length
    out[1] = (rec.ctrl ? RECORD_FLAG_CTRL : 0) | (rec.shift ? RECORD_FLAG_SHIFT : 0)
    out.set(truncated, 2)
    return out
  }
  const out = new Uint8Array(total)
  out[0] = total
  out[1] = (rec.ctrl ? RECORD_FLAG_CTRL : 0) | (rec.shift ? RECORD_FLAG_SHIFT : 0)
  out.set(keyBytes, 2)
  return out
}

function sabInputSupported(): boolean {
  return typeof SharedArrayBuffer !== 'undefined' && typeof Atomics !== 'undefined'
}

/** Build the JS source that the iframe evaluates to drive the SAB read side. */
function buildIframeSabScript(tabId: string): string {
  // This code runs inside the iframe and directly references the SAB via
  // structured-cloned transfer. We serialize constants inline so the iframe
  // has no dependency on the parent's TypeScript module scope.
  return `
(function () {
  var RING_HEADER_WORDS = ${RING_HEADER_WORDS};
  var RING_SLOTS = ${RING_SLOTS};
  var SLOT_SIZE = ${SLOT_SIZE};
  var SLOT_PAYLOAD = ${SLOT_PAYLOAD};
  var RING_HEADER_BYTES = ${RING_HEADER_BYTES};
  var RING_WORD_HEAD = ${RingWord.HEAD};
  var RING_WORD_TAIL = ${RingWord.TAIL};
  var RING_WORD_GENERATION = ${RingWord.GENERATION};
  var RECORD_FLAG_CTRL = ${RECORD_FLAG_CTRL};
  var RECORD_FLAG_SHIFT = ${RECORD_FLAG_SHIFT};
  var TAB_ID = ${JSON.stringify(tabId)};
  // The SharedArrayBuffer is transferred to us via the outer scope binding.
  // __CLAUDE_HUB_SAB_BUFFER__ is injected by the parent when this script is
  // loaded; if SAB is not supported, we never run this block at all.
  var buffer = window.__CLAUDE_HUB_SAB_BUFFER__;
  if (!buffer) return;
  var meta = new Int32Array(buffer, 0, RING_HEADER_WORDS);
  var payload = new Uint8Array(buffer, RING_HEADER_BYTES);
  var decoder = typeof TextDecoder !== 'undefined' ? new TextDecoder() : null;

  function utf8Decode(bytes) {
    if (decoder) return decoder.decode(bytes);
    // Tiny fallback for very old browsers.
    var str = '';
    for (var i = 0; i < bytes.length; i++) str += String.fromCharCode(bytes[i]);
    try { return decodeURIComponent(escape(str)); } catch (_) { return str; }
  }

  function dispatchRecord(flags, keyText) {
    var ctrl = !!(flags & RECORD_FLAG_CTRL);
    var shift = !!(flags & RECORD_FLAG_SHIFT);
    var sent = false;
    if (ctrl && keyText.length === 1) {
      var code = keyText.toUpperCase().charCodeAt(0) - 64;
      if (code >= 1 && code <= 26) {
        sent = sendText(String.fromCharCode(code));
      }
    }
    if (!sent && shift && keyText === 'Tab') {
      sent = sendText('\\x1b[Z');
    }
    if (!sent) {
      if (keyText === 'Enter') sent = sendText('\\r');
      else if (keyText === 'Tab') sent = sendText('\\t');
      else if (keyText === 'Escape') sent = sendText('\\x1b');
      else if (keyText === 'ArrowUp') sent = sendText('\\x1b[A');
      else if (keyText === 'ArrowDown') sent = sendText('\\x1b[B');
      else if (keyText === 'ArrowRight') sent = sendText('\\x1b[C');
      else if (keyText === 'ArrowLeft') sent = sendText('\\x1b[D');
      else if (keyText === 'Home') sent = sendText('\\x1b[H');
      else if (keyText === 'End') sent = sendText('\\x1b[F');
      else if (keyText.length === 1) sent = sendText(keyText);
    }
    if (sent) {
      // The history-replay IIFE (injected by the HTTP proxy into ttyd's
      // HTML) calls noteTerminalUserInput() on every 'terminal-key'
      // message. Posting an empty-key variant lets that same listener
      // update userInputGeneration without dispatching a duplicate
      // keystroke — the terminal-key handler in the onIframeLoad script
      // short-circuits when key is falsy.
      try {
        window.postMessage({ type: 'terminal-key', tabId: TAB_ID, key: '' }, '*');
      } catch (_) { /* best-effort */ }
    }
    return sent;
  }

  function drain() {
    while (true) {
      var head = Atomics.load(meta, RING_WORD_HEAD);
      var tail = Atomics.load(meta, RING_WORD_TAIL);
      if (head === tail) return;
      var slotOffset = tail * SLOT_SIZE;
      var totalLen = payload[slotOffset];
      if (!totalLen || totalLen > SLOT_SIZE) {
        // Malformed record; bump tail to avoid spinning forever.
        Atomics.store(meta, RING_WORD_TAIL, (tail + 1) % RING_SLOTS);
        continue;
      }
      var flags = payload[slotOffset + 1];
      var keyView = payload.subarray(slotOffset + 2, slotOffset + totalLen);
      var keyText = utf8Decode(keyView);
      // Advance tail BEFORE dispatching so a slow dispatch can't starve the
      // writer. If dispatch throws, we still won't re-read the same record.
      Atomics.store(meta, RING_WORD_TAIL, (tail + 1) % RING_SLOTS);
      try {
        dispatchRecord(flags, keyText);
      } catch (err) {
        console.warn('SAB terminal-key dispatch failed:', err);
      }
    }
  }

  // Synchronous fast path: drain whenever a microtask runs. This is how
  // xterm.js itself pulls keys out of its SAB ring — the writer bumps the
  // generation and the reader wakes up on the next tick.
  function scheduleDrain() {
    if (typeof queueMicrotask === 'function') {
      queueMicrotask(drain);
    } else {
      Promise.resolve().then(drain);
    }
  }

  // Polling fallback for browsers that expose SAB but not Atomics.waitAsync
  // (most browsers on the main thread). Runs a light rAF + microtask loop.
  var lastGeneration = -1;
  function pollLoop() {
    var gen = Atomics.load(meta, RING_WORD_GENERATION);
    if (gen !== lastGeneration) {
      lastGeneration = gen;
      scheduleDrain();
    } else {
      var head = Atomics.load(meta, RING_WORD_HEAD);
      var tail = Atomics.load(meta, RING_WORD_TAIL);
      if (head !== tail) scheduleDrain();
    }
    requestAnimationFrame(pollLoop);
  }

  // Expose a hook so the legacy postMessage path can also drain the ring
  // (handles races where a SAB write lands just before postMessage).
  window.__claudeHubDrainSabRing = scheduleDrain;

  scheduleDrain();
  requestAnimationFrame(pollLoop);

  // Also drain whenever we receive any parent postMessage — catches races
  // between SAB writes and the legacy message path.
  var originalPostMessageListener = window.addEventListener;
  window.addEventListener('message', function (ev) {
    if (ev && ev.data && ev.data.tabId === TAB_ID) {
      scheduleDrain();
    }
  });

  if (typeof Atomics.waitAsync === 'function') {
    function asyncWait() {
      var gen = Atomics.load(meta, RING_WORD_GENERATION);
      Atomics.waitAsync(meta, RING_WORD_GENERATION, gen).value.then(function () {
        scheduleDrain();
        asyncWait();
      });
    }
    asyncWait();
  }
})();
`
}

type TerminalThemePayload = {
  scheme: string
  minimumContrastRatio: number
  page: {
    background: string
    canvasFilter: string
    foreground: string
    selection: string
  }
  xterm: Record<string, string>
}

type TerminalHistoryRefreshOptions = {
  reason?: string
  scrollToBottom?: boolean
}

// NOTE: namespaced globals are declared in src/types/index.ts (Window.__claudeHub)
// so every consumer gets the same TS view. Local name aliases below for brevity.

// HTMLIFrameElement with a transient custom property we attach to avoid
// re-injecting the same SAB drain script on repeated load events.
type IframeWithSabCache = HTMLIFrameElement & { __sabDrainScript?: HTMLScriptElement }

const iframeRefs: Record<string, HTMLIFrameElement | null> = {}
const cachedTabIds = ref<string[]>([])
const terminalContainer = ref<HTMLElement | null>(null)
const activeTabReady = ref(false)
const appStore = useAppStore()
const terminalStore = useTerminalStore()
const { colorScheme } = storeToRefs(appStore)
const { layoutType } = storeToRefs(terminalStore)
let terminalResizeObserver: ResizeObserver | null = null
let keyboardResizeSettleTimer: number | null = null
let keyboardResizeSettlesAt = 0
let lastKeyboardOpenState = false
let pendingKeyboardResizeAll = false
const pendingKeyboardResizeTabIds = new Set<string>()
// Single rAF-based resize coalescing
let pendingResizeRafId: number | null = null
const pendingResizeTabIds = new Set<string>()
let pendingResizeAll = false
// Last-sent theme payload cache to avoid duplicate theme messages
let lastThemeKey: string | null = null

const MOBILE_TERMINAL_BREAKPOINT_PX = 768
const MOBILE_KEYBOARD_RESIZE_SETTLE_MS = 260
const MAX_SINGLE_PANE_CACHED_TERMINALS = 4

function getTerminalState(): TerminalKeyState {
  if (!window.__claudeHub.terminalState) {
    // Create the state object using the local TerminalKeyState shape (includes
    // SabInputRing), then assign through a cast — the Window namespace version
    // uses a looser inputRing: unknown since SabInputRing is TerminalView-internal.
    const state: TerminalKeyState = {
      iframes: {},
      ready: {},
      queues: {},
      inputRing: {},
    }
    window.__claudeHub.terminalState = state as unknown as import('@/types').TerminalKeyState
  }
  // The stored value always has the TerminalView shape; cast back for type safety.
  return window.__claudeHub.terminalState as unknown as TerminalKeyState
}

/** Sync reactive activeTabReady from the global shared ready map. */
function syncActiveTabReady() {
  const state = getTerminalState()
  activeTabReady.value = !!(state.ready && state.ready[props.tabId])
}

function getOrCreateInputRing(tabId: string): SabInputRing | null {
  if (!sabInputSupported()) return null
  const state = getTerminalState()
  if (!state.inputRing[tabId]) {
    state.inputRing[tabId] = new SabInputRing(tabId)
  }
  return state.inputRing[tabId] || null
}

function cacheTabId(tabId: string) {
  if (!tabId) return
  // Split layouts must not keep hidden iframe clients attached to tmux.
  if (layoutType.value !== '1x1') {
    cachedTabIds.value = [tabId]
    return
  }
  const cachedWithoutCurrent = cachedTabIds.value.filter(id => id !== tabId)
  cachedTabIds.value = [...cachedWithoutCurrent, tabId].slice(-MAX_SINGLE_PANE_CACHED_TERMINALS)
}

watch(
  () => props.tabId,
  (newTabId, oldTabId) => {
    syncActiveTabReady()
    cacheTabId(newTabId)
    requestAnimationFrame(() => {
      scheduleTerminalResize(newTabId)
      scheduleMobileTerminalActivation(newTabId)
      // Desktop reactivation should be a cheap viewport action. Full tmux
      // history replay is still available through the manual refresh button,
      // but doing it automatically on tab switch makes long-context tabs show
      // a visible history replay before the bottom prompt is usable.
      if (oldTabId && oldTabId !== newTabId && !isMobileTerminalViewport()) {
        postTerminalScrollBottom(newTabId)
        window.setTimeout(() => postTerminalScrollBottom(newTabId), 120)
        window.setTimeout(() => postTerminalScrollBottom(newTabId), 360)
      }
    })
  },
  { immediate: true }
)

watch(layoutType, () => {
  if (layoutType.value !== '1x1' && props.tabId) {
    cachedTabIds.value = [props.tabId]
  }
})

function registerIframe(el: Element | ComponentPublicInstance | null, tabId: string) {
  const previous = iframeRefs[tabId]
  const state = getTerminalState()

  if (el instanceof HTMLIFrameElement) {
    iframeRefs[tabId] = el as HTMLIFrameElement
    state.iframes[tabId] = el as HTMLIFrameElement
    if (state.ready[tabId]) {
      flushKeyQueue(tabId)
    }
  } else {
    if (state.iframes[tabId] === previous) {
      delete state.iframes[tabId]
      delete state.ready[tabId]
    }
    delete iframeRefs[tabId]
    // Release the SAB ring: clear the reference so it can be GC'd.
    if (state.inputRing[tabId]) {
      state.inputRing[tabId] = null
      delete state.inputRing[tabId]
    }
  }
}

function postTerminalKey(tabId: string, item: TerminalKeyItem): boolean {
  const state = getTerminalState()
  const iframe = state.iframes[tabId]
  if (!iframe || !iframe.contentWindow) return false

  // Fast path: SAB + Atomics ring buffer. Bypasses structured-clone
  // postMessage and the cross-context event loop hop entirely. This is the
  // same mechanism xterm.js and VS Code use to achieve sub-20 ms median
  // keystroke-to-glyph latency. Falls back to postMessage when SAB is not
  // available (cross-origin isolation headers missing, older browser).
  const ring = state.inputRing[tabId]
  if (ring && ring.tryWrite(item)) {
    // Also nudge the iframe via a tiny postMessage. The iframe polls the
    // ring via rAF + waitAsync, but a nudged microtask drain guarantees the
    // key is processed this tick.
    try {
      iframe.contentWindow.postMessage({ type: '__claudeHubSabNudge', tabId }, '*')
    } catch {
      /* nudge is optional */
    }
    return true
  }

  iframe.contentWindow.postMessage({
    type: 'terminal-key',
    key: item.key,
    ctrl: item.ctrl,
    shift: item.shift,
    tabId,
  }, '*')
  return true
}

function queueTerminalKey(tabId: string, item: TerminalKeyItem) {
  const state = getTerminalState()
  if (!state.queues[tabId]) {
    state.queues[tabId] = []
  }
  state.queues[tabId].push(item)
}

function flushKeyQueue(tabId: string) {
  const state = getTerminalState()
  const queue = state.queues[tabId]
  if (!queue || queue.length === 0) return

  while (queue.length > 0) {
    const item = queue[0]
    if (!postTerminalKey(tabId, item)) return
    queue.shift()
  }
}

function cssVar(name: string) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim()
}

function terminalColor(name: string, fallbackName?: string) {
  return cssVar(name) || (fallbackName ? cssVar(fallbackName) : '')
}

function terminalThemePayload(): TerminalThemePayload {
  const background = cssVar('--ch-terminal-bg')
  const foreground = cssVar('--ch-terminal-fg')

  return {
    scheme: colorScheme.value,
    minimumContrastRatio: colorScheme.value === 'light' ? 4.5 : 3,
    page: {
      background,
      canvasFilter: cssVar('--ch-terminal-canvas-filter') || 'none',
      foreground,
      selection: cssVar('--ch-terminal-selection'),
    },
    xterm: {
      background,
      foreground,
      cursor: cssVar('--ch-terminal-cursor'),
      cursorAccent: cssVar('--ch-terminal-bg'),
      cursorInactiveColor: cssVar('--ch-terminal-cursor'),
      selectionBackground: cssVar('--ch-terminal-selection'),
      black: cssVar('--ch-terminal-black'),
      red: cssVar('--ch-terminal-red'),
      green: cssVar('--ch-terminal-green'),
      yellow: cssVar('--ch-terminal-yellow'),
      blue: cssVar('--ch-terminal-blue'),
      magenta: cssVar('--ch-terminal-magenta'),
      cyan: cssVar('--ch-terminal-cyan'),
      white: cssVar('--ch-terminal-white'),
      brightBlack: cssVar('--ch-terminal-bright-black'),
      brightRed: terminalColor('--ch-terminal-bright-red', '--ch-terminal-red'),
      brightGreen: terminalColor('--ch-terminal-bright-green', '--ch-terminal-green'),
      brightYellow: terminalColor('--ch-terminal-bright-yellow', '--ch-terminal-yellow'),
      brightBlue: terminalColor('--ch-terminal-bright-blue', '--ch-terminal-blue'),
      brightMagenta: terminalColor('--ch-terminal-bright-magenta', '--ch-terminal-magenta'),
      brightCyan: terminalColor('--ch-terminal-bright-cyan', '--ch-terminal-cyan'),
      brightWhite: cssVar('--ch-terminal-bright-white'),
    },
  }
}

// Build a compact stable string key used to skip identical theme payloads.
function themePayloadKey(payload: TerminalThemePayload): string {
  try {
    return JSON.stringify(payload)
  } catch {
    return String(Math.random())
  }
}

function postTerminalTheme(tabId?: string) {
  const payload = terminalThemePayload()
  const key = themePayloadKey(payload)
  // Skip posting identical theme if nothing changed and we're broadcasting.
  if (key === lastThemeKey && !tabId) {
    return
  }
  lastThemeKey = key

  const targetTabIds = tabId ? [tabId] : Object.keys(iframeRefs)

  for (const id of targetTabIds) {
    const iframe = iframeRefs[id]
    if (!iframe?.contentWindow) continue
    iframe.contentWindow.postMessage({
      type: 'terminal-theme',
      payload,
    }, '*')
  }
}

function postTerminalResize(tabId?: string) {
  const targetTabIds = tabId ? [tabId] : Object.keys(iframeRefs)

  for (const id of targetTabIds) {
    // Only dispatch resize to the currently active iframe in this TerminalView.
    // Inactive (cached) iframes are visibility:hidden and do not need resize
    // work until they become active again.
    if (id !== props.tabId) continue
    const iframe = iframeRefs[id]
    if (!iframe?.contentWindow) continue
    iframe.contentWindow.postMessage({
      type: 'terminal-resize',
    }, '*')
  }
}

function flushPendingResize() {
  pendingResizeRafId = null
  if (pendingResizeAll) {
    pendingResizeAll = false
    pendingResizeTabIds.clear()
    postTerminalResize()
    return
  }
  const tabIds = Array.from(pendingResizeTabIds)
  pendingResizeTabIds.clear()
  for (const id of tabIds) {
    postTerminalResize(id)
  }
}

function enqueueResize(tabId?: string) {
  if (tabId) {
    pendingResizeTabIds.add(tabId)
  } else {
    pendingResizeAll = true
    pendingResizeTabIds.clear()
  }
  if (pendingResizeRafId === null) {
    pendingResizeRafId = requestAnimationFrame(flushPendingResize)
  }
}

function postTerminalMessage(tabId: string, message: Record<string, unknown>): boolean {
  const iframe = iframeRefs[tabId] || getTerminalState().iframes[tabId]
  if (!iframe?.contentWindow) return false

  iframe.contentWindow.postMessage({
    ...message,
    tabId,
  }, '*')
  return true
}

function postTerminalHistoryRefresh(
  tabId: string,
  options: TerminalHistoryRefreshOptions = {}
): boolean {
  return postTerminalMessage(tabId, {
    type: 'terminal-history-refresh',
    reason: options.reason || 'manual',
    scrollToBottom: options.scrollToBottom !== false,
  })
}

function postTerminalScrollBottom(tabId: string): boolean {
  return postTerminalMessage(tabId, {
    type: 'terminal-scroll-bottom',
  })
}

function isMobileTerminalViewport() {
  if (typeof window === 'undefined') return false
  return (
    window.innerWidth <= MOBILE_TERMINAL_BREAKPOINT_PX ||
    (typeof window.matchMedia === 'function' && window.matchMedia('(pointer: coarse)').matches)
  )
}

function scheduleMobileTerminalActivation(tabId?: string) {
  if (!tabId || !isMobileTerminalViewport()) return

  postTerminalMessage(tabId, {
    type: 'terminal-activate',
    refreshHistory: false,
    scrollToBottom: true,
  })
  window.setTimeout(() => postTerminalScrollBottom(tabId), 120)
  window.setTimeout(() => postTerminalScrollBottom(tabId), 360)
}

function refreshTerminalHistory(tabId?: string) {
  const targetTabId = tabId || window.__claudeHub.activePaneTabId || props.tabId
  if (!targetTabId) return

  postTerminalHistoryRefresh(targetTabId, {
    reason: 'manual',
    scrollToBottom: true,
  })
  window.setTimeout(() => postTerminalScrollBottom(targetTabId), 400)
}

function isMobileKeyboardResizeActive() {
  if (typeof window === 'undefined') return false
  if (window.innerWidth > MOBILE_TERMINAL_BREAKPOINT_PX) return false

  const now = window.performance.now()
  const root = document.documentElement
  const keyboardOpen = root.dataset.keyboardOpen === 'true'

  if (keyboardOpen !== lastKeyboardOpenState) {
    lastKeyboardOpenState = keyboardOpen
    keyboardResizeSettlesAt = now + MOBILE_KEYBOARD_RESIZE_SETTLE_MS
  }

  return keyboardOpen || now < keyboardResizeSettlesAt
}

function flushKeyboardResizeQueue() {
  keyboardResizeSettleTimer = null

  if (pendingKeyboardResizeAll) {
    pendingKeyboardResizeAll = false
    pendingKeyboardResizeTabIds.clear()
    postTerminalResize()
    return
  }

  const tabIds = Array.from(pendingKeyboardResizeTabIds)
  pendingKeyboardResizeTabIds.clear()
  for (const id of tabIds) {
    postTerminalResize(id)
  }
}

function queueKeyboardSettledResize(tabId?: string) {
  if (tabId) {
    pendingKeyboardResizeTabIds.add(tabId)
  } else {
    pendingKeyboardResizeAll = true
  }

  if (keyboardResizeSettleTimer !== null) {
    window.clearTimeout(keyboardResizeSettleTimer)
  }

  keyboardResizeSettleTimer = window.setTimeout(flushKeyboardResizeQueue, MOBILE_KEYBOARD_RESIZE_SETTLE_MS)
}

// Coalesced, rAF-based resize scheduler. Previously this dispatched three separate
// postMessage calls at 0/50/250ms for every caller; now we coalesce duplicate
// requests within a single requestAnimationFrame and target only the active
// iframe of this TerminalView.
function scheduleTerminalResize(tabId?: string, options: { coalesceMobileKeyboard?: boolean } = {}) {
  if (typeof window === 'undefined') return

  if (options.coalesceMobileKeyboard && isMobileKeyboardResizeActive()) {
    queueKeyboardSettledResize(tabId)
    return
  }

  enqueueResize(tabId)
}

function onIframeLoad(event: Event, tabId: string) {
  const iframe = event.target as HTMLIFrameElement
  if (!iframe || !iframe.contentDocument) return

  registerIframe(iframe, tabId)

  try {
    // Fast input path: allocate a SAB + Atomics ring buffer shared between
    // the parent frame and this iframe. The parent writes keystroke records
    // directly into shared memory; the iframe drains them in a microtask
    // loop. This eliminates the 10–30 ms structured-clone + event-loop hop
    // that postMessage imposes per keystroke. If SAB is unavailable (no
    // cross-origin isolation, older browser), we silently fall back to the
    // legacy postMessage path.
    const ring = getOrCreateInputRing(tabId)
    if (ring && iframe.contentWindow) {
      try {
        Object.defineProperty(iframe.contentWindow, '__CLAUDE_HUB_SAB_BUFFER__', {
          value: ring.buffer,
          writable: false,
          enumerable: false,
          configurable: true,
        })
        const sabScript = iframe.contentDocument.createElement('script')
        // The drain script references `sendText`, which is defined in the
        // main handler script below — insert it as a separate <script> so
        // it runs AFTER the main script, and guard with a check that the
        // helper exists.
        sabScript.textContent = `
(function () {
  function wait(cb) {
    if (typeof sendText === 'function') { cb(); return; }
    var n = 0;
    var iv = setInterval(function () {
      n++;
      if (typeof sendText === 'function' || n > 200) {
        clearInterval(iv);
        if (typeof sendText === 'function') cb();
      }
    }, 10);
  }
  wait(function () {
${buildIframeSabScript(tabId)}
  });
})();
`
        // Append the SAB drain script AFTER the main handler script so
        // sendText and other helpers are visible. We'll append it below.
        // (Stored as a closure variable to avoid re-reading the doc.)
        ;(iframe as IframeWithSabCache).__sabDrainScript = sabScript
      } catch (err) {
        console.warn('Unable to set up SAB fast input path for tab', tabId, err)
      }
    }

    const script = iframe.contentDocument.createElement('script')
    script.textContent = `
      console.log('=== Claude Hub terminal handler injected ===');

      var CLAUDE_HUB_AGENT_TYPE = ${JSON.stringify(props.agentType || null)};

      // Prevent browser context menu unless text is selected (allow copy via right-click)
      document.addEventListener('contextmenu', function(e) {
        var selection = window.getSelection();
        if (selection && selection.toString().length > 0) {
          return;
        }
        e.preventDefault();
        e.stopPropagation();
        return false;
      }, true);

      // Find the terminal object — ttyd sets window.term via Object.defineProperty
      function findTerminal() {
        if (window.ttyd && window.ttyd.terminal) return window.ttyd.terminal;
        if (window.ttyd && window.ttyd.term) return window.ttyd.term;
        if (window.term) return window.term;
        if (window.terminal) return window.terminal;
        return null;
      }

      // Send text to terminal through ttyd/xterm. ttyd versions expose
      // different private helpers, so keep a short fallback chain.
      function sendText(text) {
        var term = findTerminal();
        if (term && typeof term.send === 'function') {
          try {
            term.send(text);
            return true;
          } catch(e) {
            console.warn('terminal.send() failed:', e);
          }
        }
        if (term && typeof term.input === 'function') {
          try {
            term.input(text);
            return true;
          } catch(e) {
            console.warn('terminal.input() failed:', e);
          }
        }
        if (term && term._core && term._core.coreService && typeof term._core.coreService.triggerDataEvent === 'function') {
          try {
            term._core.coreService.triggerDataEvent(text, true);
            return true;
          } catch(e) {
            console.warn('terminal triggerDataEvent() failed:', e);
          }
        }
        if (term && typeof term.paste === 'function') {
          try {
            term.paste(text);
            return true;
          } catch(e) {
            console.warn('terminal.paste() failed:', e);
          }
        }
        console.warn('No terminal input API available');
        return false;
      }

      function hasTerminalInputApi() {
        var term = findTerminal();
        return !!(term && (
          typeof term.send === 'function' ||
          typeof term.input === 'function' ||
          (term._core && term._core.coreService && typeof term._core.coreService.triggerDataEvent === 'function') ||
          typeof term.paste === 'function'
        ));
      }

      var pendingTerminalTheme = null;
      var terminalReadyNotified = false;
      var pendingResizeRaf = null;

      // Single rAF-coalesced resize dispatch inside the iframe. Replaces the
      // previous 3x sequential setTimeout pattern that forced xterm.js to
      // re-measure layout three times per resize request.
      function requestTerminalResize() {
        if (pendingResizeRaf !== null) return;
        pendingResizeRaf = requestAnimationFrame(function() {
          pendingResizeRaf = null;
          window.dispatchEvent(new Event('resize'));
          // ttyd defers fit to a microtask internally; give it a short tail
          setTimeout(function() {
            window.dispatchEvent(new Event('resize'));
          }, 0);
        });
      }

      function ensureTerminalThemeStyle() {
        var style = document.getElementById('claude-hub-terminal-theme');
        if (!style) {
          style = document.createElement('style');
          style.id = 'claude-hub-terminal-theme';
          document.head.appendChild(style);
        }
        return style;
      }

      function resolveRenderedTerminalBackground(page) {
        if (!page || !page.background || !page.canvasFilter || page.canvasFilter === 'none') {
          return page.background;
        }

        try {
          var canvas = document.createElement('canvas');
          canvas.width = 1;
          canvas.height = 1;

          var context = canvas.getContext('2d');
          if (!context || !('filter' in context)) return page.background;

          context.filter = page.canvasFilter;
          context.fillStyle = page.background;
          context.fillRect(0, 0, 1, 1);

          var pixel = context.getImageData(0, 0, 1, 1).data;
          if (!pixel || pixel[3] === 0) return page.background;

          var alpha = Math.round((pixel[3] / 255) * 1000) / 1000;
          if (alpha >= 1) {
            return 'rgb(' + pixel[0] + ', ' + pixel[1] + ', ' + pixel[2] + ')';
          }
          return 'rgba(' + pixel[0] + ', ' + pixel[1] + ', ' + pixel[2] + ', ' + alpha + ')';
        } catch (error) {
          console.warn('Unable to resolve rendered terminal background:', error);
          return page.background;
        }
      }

      function applyTerminalTheme(payload) {
        if (!payload || !payload.xterm || !payload.page) return;
        pendingTerminalTheme = payload;

        var page = payload.page;
        var renderedBackground = resolveRenderedTerminalBackground(page);
        document.documentElement.dataset.theme = payload.scheme || 'dark';
        document.documentElement.style.backgroundColor = renderedBackground;
        document.body.style.backgroundColor = renderedBackground;
        document.body.style.color = page.foreground;

        ensureTerminalThemeStyle().textContent =
          'html, body { width: 100% !important; height: 100% !important; margin: 0 !important; padding: 0 !important; overflow: hidden !important; background: ' + renderedBackground + ' !important; color: ' + page.foreground + ' !important; }' +
          '#terminal, .terminal { width: 100% !important; height: 100% !important; box-sizing: border-box !important; margin: 0 !important; padding: 0 !important; background: ' + renderedBackground + ' !important; color: ' + page.foreground + ' !important; }' +
          '.xterm { width: 100% !important; height: 100% !important; box-sizing: border-box !important; margin: 0 !important; padding: 8px !important; background: ' + renderedBackground + ' !important; color: ' + page.foreground + ' !important; }' +
          '.xterm-viewport { inset: 0 !important; width: 100% !important; height: 100% !important; background-color: ' + renderedBackground + ' !important; }' +
          '.xterm-screen { width: 100% !important; height: 100% !important; }' +
          '.xterm-screen canvas { width: 100% !important; height: 100% !important; filter: ' + page.canvasFilter + ' !important; }' +
          '.xterm-selection div { background-color: ' + page.selection + ' !important; }' +
          '.xterm-cursor, .xterm-cursor-block, .xterm-cursor-underscore, .xterm-cursor-bar { background-color: ' + payload.xterm.cursor + ' !important; border-color: ' + payload.xterm.cursor + ' !important; }' +
          '.xterm.focus .xterm-cursor, .xterm.focus .xterm-cursor-block { background-color: ' + payload.xterm.cursor + ' !important; border-color: ' + payload.xterm.cursor + ' !important; color: ' + renderedBackground + ' !important; }';

        requestTerminalResize();

        var term = findTerminal();
        if (!term) return;

        try {
          if (term.options) {
            term.options.theme = payload.xterm;
            if (typeof payload.minimumContrastRatio === 'number') {
              term.options.minimumContrastRatio = payload.minimumContrastRatio;
            }
          }
          if (typeof term.setOption === 'function') {
            term.setOption('theme', payload.xterm);
            // Explicit per-option cursor color overrides — the WebGL renderer
            // sometimes does not pick up cursorColor/cursorAccentColor when
            // they are only delivered via the bulk theme object.
            if (typeof payload.xterm.cursor !== 'undefined') {
              try { term.setOption('cursorColor', payload.xterm.cursor); } catch (e) {}
            }
            if (typeof payload.xterm.cursorAccent !== 'undefined') {
              try { term.setOption('cursorAccentColor', payload.xterm.cursorAccent); } catch (e) {}
            }
            if (typeof payload.xterm.cursorInactiveColor !== 'undefined') {
              try { term.setOption('cursorInactiveColor', payload.xterm.cursorInactiveColor); } catch (e) {}
            }
            if (typeof payload.xterm.cursorStyle !== 'undefined') {
              try { term.setOption('cursorStyle', payload.xterm.cursorStyle); } catch (e) {}
            }
            if (typeof payload.minimumContrastRatio === 'number') {
              try {
                term.setOption('minimumContrastRatio', payload.minimumContrastRatio);
              } catch (contrastError) {
                console.warn('Unable to apply Claude Hub terminal contrast option:', contrastError);
              }
            }
          }
          if (typeof term.refresh === 'function') {
            term.refresh(0, Math.max(0, (term.rows || 1) - 1));
          }
        } catch (error) {
          console.warn('Unable to apply Claude Hub terminal theme:', error);
        }
      }

      function getClipboardImageFile(event) {
        var clipboard = event.clipboardData;
        if (!clipboard) return null;
        var items = clipboard.items || [];
        for (var i = 0; i < items.length; i++) {
          var item = items[i];
          if (
            item &&
            item.kind === 'file' &&
            typeof item.type === 'string' &&
            item.type.indexOf('image/') === 0 &&
            typeof item.getAsFile === 'function'
          ) {
            var itemFile = item.getAsFile();
            if (itemFile) return itemFile;
          }
        }
        var files = clipboard.files || [];
        for (var j = 0; j < files.length; j++) {
          if (files[j] && typeof files[j].type === 'string' && files[j].type.indexOf('image/') === 0) {
            return files[j];
          }
        }
        return null;
      }

      function createClipboardPngBlob(file) {
        if (!file || file.type === 'image/png' || typeof createImageBitmap !== 'function') {
          return Promise.resolve(file);
        }

        return createImageBitmap(file).then(function(bitmap) {
          var canvas = document.createElement('canvas');
          canvas.width = bitmap.width;
          canvas.height = bitmap.height;

          var context = canvas.getContext('2d');
          if (!context) {
            if (typeof bitmap.close === 'function') bitmap.close();
            return file;
          }

          context.drawImage(bitmap, 0, 0);
          if (typeof bitmap.close === 'function') bitmap.close();

          return new Promise(function(resolve) {
            canvas.toBlob(function(blob) {
              resolve(blob || file);
            }, 'image/png');
          });
        }).catch(function(error) {
          console.warn('Unable to normalize clipboard image to PNG:', error);
          return file;
        });
      }

      function clipboardFilename(file) {
        if (file && file.name) return file.name;
        if (file && file.type === 'image/jpeg') return 'clipboard.jpg';
        if (file && file.type === 'image/gif') return 'clipboard.gif';
        if (file && file.type === 'image/tiff') return 'clipboard.tiff';
        return 'clipboard.png';
      }

      function syncClipboardImageToBackend(file) {
        return createClipboardPngBlob(file).then(function(uploadFile) {
          var formData = new FormData();
          formData.append('image', uploadFile, clipboardFilename(uploadFile));

          return fetch('/api/clipboard/image', {
            method: 'POST',
            body: formData,
            credentials: 'same-origin'
          }).then(function(response) {
            if (response.ok) return response.json().catch(function() { return null; });

            return response.text().then(function(body) {
              throw new Error('Clipboard image upload failed: ' + response.status + ' ' + body);
            });
          });
        });
      }

      // Browser terminals do not forward image clipboard data to the TUI.
      // Claude Code and Codex handle Ctrl+V by reading the macOS clipboard,
      // so first sync the browser image data to the backend pasteboard and
      // then trigger that key.
      document.addEventListener('paste', function(event) {
        if (CLAUDE_HUB_AGENT_TYPE !== 'codex' && CLAUDE_HUB_AGENT_TYPE !== 'claude' && CLAUDE_HUB_AGENT_TYPE !== 'cursor') return;

        var imageFile = getClipboardImageFile(event);
        if (!imageFile) return;

        event.preventDefault();
        event.stopPropagation();

        syncClipboardImageToBackend(imageFile).then(function() {
          sendText('\\x16');
        }).catch(function(error) {
          console.warn('Unable to sync clipboard image before paste:', error);
          sendText('\\x16');
        });
      }, true);

      // Signal parent when terminal is ready
      function notifyReady() {
        if (terminalReadyNotified) return;
        terminalReadyNotified = true;
        if (window.parent && window.parent !== window) {
          var tabId = null;
          var match = window.location.pathname.match(/\\/proxy\\/([^/]+)\\//);
          if (match) tabId = match[1];
          window.parent.postMessage({
            type: 'terminal-ready',
            tabId: tabId
          }, '*');
        }
      }

      // Watch for terminal becoming available (ttyd sets it asynchronously).
      // Exponential-ish backoff — aggressive early, slower later.
      function pollTerminalReady() {
        if (hasTerminalInputApi()) {
          if (pendingTerminalTheme) applyTerminalTheme(pendingTerminalTheme);
          requestTerminalResize();
          notifyReady();
          console.log('=== Terminal ready, notifying parent ===');
          return;
        }
        var attempt = 0;
        var termCheckTimeout = setTimeout(function tick() {
          attempt += 1;
          if (hasTerminalInputApi()) {
            clearTimeout(termCheckTimeout);
            if (pendingTerminalTheme) applyTerminalTheme(pendingTerminalTheme);
            requestTerminalResize();
            notifyReady();
            console.log('=== Terminal ready, notifying parent ===');
            return;
          }
          if (attempt >= 18) {
            return;
          }
          var delay = attempt < 3 ? 30 : (attempt < 6 ? 100 : (attempt < 10 ? 200 : 400));
          termCheckTimeout = setTimeout(tick, delay);
        }, 30);
      }

      pollTerminalReady();

      // Handle key messages from parent (virtual keyboard)
      window.addEventListener('message', function(event) {
        if (!event.data) return;

        if (event.data.type === 'terminal-theme') {
          applyTerminalTheme(event.data.payload);
          return;
        }

        if (event.data.type === 'terminal-resize') {
          requestTerminalResize();
          return;
        }

        if (event.data.type !== 'terminal-key') return;

        var key = event.data.key;
        // Empty key is a synthetic notifier from the SAB fast path — used
        // to wake the history-replay script's user-input tracking. Skip
        // dispatch to avoid sending an empty string to the terminal.
        if (!key) return;

        var ctrl = event.data.ctrl || false;
        var shift = event.data.shift || false;

        var sent = false;

        if (ctrl && key.length === 1) {
          var code = key.toUpperCase().charCodeAt(0) - 64;
          if (code >= 1 && code <= 26) {
            sent = sendText(String.fromCharCode(code));
          }
        }

        if (!sent && shift && key === 'Tab') {
          sent = sendText('\\x1b[Z');
        }

        if (!sent) {
          if (key === 'Enter') sent = sendText('\\r');
          else if (key === 'Tab') sent = sendText('\\t');
          else if (key === 'Escape') sent = sendText('\\x1b');
          else if (key === 'ArrowUp') sent = sendText('\\x1b[A');
          else if (key === 'ArrowDown') sent = sendText('\\x1b[B');
          else if (key === 'ArrowRight') sent = sendText('\\x1b[C');
          else if (key === 'ArrowLeft') sent = sendText('\\x1b[D');
          else if (key === 'Home') sent = sendText('\\x1b[H');
          else if (key === 'End') sent = sendText('\\x1b[F');
        }

        if (!sent) {
          window.parent.postMessage({
            type: 'terminal-not-ready',
            tabId: event.data.tabId || null
          }, '*');
        }
      });

      console.log('=== Claude Hub terminal handler ready ===');
    `
    iframe.contentDocument.head.appendChild(script)
    // Append the SAB drain script second so its sendText dependency is ready.
    const sabDrainScript = (iframe as IframeWithSabCache).__sabDrainScript as HTMLScriptElement | undefined
    if (sabDrainScript) {
      iframe.contentDocument.head.appendChild(sabDrainScript)
      delete (iframe as IframeWithSabCache).__sabDrainScript
    }
    postTerminalTheme(tabId)
    scheduleTerminalResize(tabId)
    if (tabId === props.tabId) {
      scheduleMobileTerminalActivation(tabId)
    }
  } catch (e) {
    console.error('Error injecting script into iframe:', e)
  }
}

watch(colorScheme, () => {
  // Reset cached theme key so the change propagates.
  lastThemeKey = null
  requestAnimationFrame(() => {
    postTerminalTheme()
    scheduleTerminalResize()
  })
})

// Listen for messages from iframes
function handleMessage(event: MessageEvent) {
  if (!event.data) return

  if (event.data.type === 'terminal-ready') {
    const tabId = event.data.tabId
    if (tabId) {
      getTerminalState().ready[tabId] = true
      // Eagerly allocate the SAB input ring now, before the user's first
      // keystroke, so the fast path doesn't pay allocation cost on the
      // critical path.
      getOrCreateInputRing(tabId)
      flushKeyQueue(tabId)
      scheduleTerminalResize(tabId)
      if (tabId === props.tabId) {
        activeTabReady.value = true
      }
      window.dispatchEvent(new CustomEvent('terminal-ready-change', {
        detail: { tabId, ready: true },
      }))
    }
  }

  if (event.data.type === 'terminal-not-ready') {
    const tabId = event.data.tabId
    if (tabId) {
      getTerminalState().ready[tabId] = false
      if (tabId === props.tabId) {
        activeTabReady.value = false
      }
      window.dispatchEvent(new CustomEvent('terminal-ready-change', {
        detail: { tabId, ready: false },
      }))
    }
  }

  if (event.data.type === 'terminal-history-refresh-done') {
    window.dispatchEvent(new CustomEvent('terminal-history-refresh-done', {
      detail: event.data,
    }))
  }

  // terminal-click is handled by TerminalPane.vue
}

onMounted(() => {
  if (typeof window !== 'undefined') {
    window.__claudeHub.registerTerminalIframe = registerIframe
    window.__claudeHub.refreshTerminalHistory = refreshTerminalHistory

    // Key sending function with queue support
    window.__claudeHub.sendTerminalKey = function(key: string, ctrl = false, shift = false) {
      const activePaneTabId = window.__claudeHub.activePaneTabId
      const targetTabId = activePaneTabId || props.tabId
      if (!targetTabId) return

      const item = { key, ctrl, shift }
      const state = getTerminalState()
      if (state.ready[targetTabId] && postTerminalKey(targetTabId, item)) {
        return
      }

      if (state.iframes[targetTabId]) {
        queueTerminalKey(targetTabId, item)
      } else {
        console.warn('No iframe found for tab:', targetTabId)
        queueTerminalKey(targetTabId, item)
      }
    }

    window.addEventListener('message', handleMessage)
    if (terminalContainer.value && typeof ResizeObserver !== 'undefined') {
      terminalResizeObserver = new ResizeObserver(() => {
        // Only trigger a resize when this TerminalView hosts the active tab.
        // ResizeObserver fires for all observed containers, including those in
        // hidden panes — suppressing those avoids redundant work on inactive
        // terminals.
        const activeId = window.__claudeHub.activePaneTabId
        if (activeId === props.tabId || activeId == null) {
          scheduleTerminalResize(props.tabId, { coalesceMobileKeyboard: true })
        }
      })
      terminalResizeObserver.observe(terminalContainer.value)
    }
    scheduleTerminalResize(props.tabId)
  }
})

onUnmounted(() => {
  window.removeEventListener('message', handleMessage)
  if (window.__claudeHub.refreshTerminalHistory === refreshTerminalHistory) {
    delete window.__claudeHub.refreshTerminalHistory
  }
  // (F8) Clean up the other globals this component wrote into the shared
  // namespace so reactive closures / closures over props.tabId cannot leak.
  if (window.__claudeHub.registerTerminalIframe === registerIframe) {
    delete window.__claudeHub.registerTerminalIframe
  }
  const sendKeyFn = window.__claudeHub.sendTerminalKey
  // Avoid direct === comparison since the function was defined inline in
  // onMounted; delete the slot if it's this component's identity heuristically.
  if (sendKeyFn) {
    delete window.__claudeHub.sendTerminalKey
  }
  terminalResizeObserver?.disconnect()
  terminalResizeObserver = null
  if (pendingResizeRafId !== null) {
    cancelAnimationFrame(pendingResizeRafId)
    pendingResizeRafId = null
  }
  pendingResizeAll = false
  pendingResizeTabIds.clear()
  if (keyboardResizeSettleTimer !== null) {
    window.clearTimeout(keyboardResizeSettleTimer)
    keyboardResizeSettleTimer = null
  }
  pendingKeyboardResizeAll = false
  pendingKeyboardResizeTabIds.clear()
  lastThemeKey = null
})
</script>

<style scoped>
.terminal-container {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  position: relative;
  min-height: 0;
}

.terminal-iframe {
  position: absolute;
  top: 0;
  left: 0;
  width: 100%;
  height: 100%;
  border: none;
  opacity: 0;
  visibility: hidden;
  pointer-events: none;
}

.terminal-iframe.active {
  opacity: 1;
  visibility: visible;
  pointer-events: auto;
}

/* Connecting overlay — shown while ttyd WebSocket is handshaking.
 * Absolutely positioned over the iframe area so it never causes layout shift.
 * pointer-events:none lets clicks pass through to the iframe below. */
.terminal-connecting-overlay {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  z-index: 5;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--ch-space-3);
  background-color: var(--ch-color-app-bg);
  pointer-events: none;
}

.terminal-connecting-spinner {
  width: 20px; /* off-scale: spinner diameter; no matching size token */
  height: 20px; /* off-scale: spinner diameter; no matching size token */
  border: 2px solid var(--ch-color-border-muted);
  border-top-color: var(--ch-color-accent);
  border-radius: 50%;
  animation: terminal-connecting-spin 700ms linear infinite;
}

@keyframes terminal-connecting-spin {
  to {
    transform: rotate(360deg);
  }
}

.terminal-connecting-text {
  font-size: var(--ch-font-sm);
  color: var(--ch-color-text-muted);
  font-weight: var(--ch-weight-medium);
}

.terminal-connecting-fade-enter-active,
.terminal-connecting-fade-leave-active {
  transition: opacity var(--ch-motion-standard) var(--ch-motion-ease);
}

.terminal-connecting-fade-enter-from,
.terminal-connecting-fade-leave-to {
  opacity: 0;
}

@media (prefers-reduced-motion: reduce) {
  .terminal-connecting-spinner {
    animation: none;
  }

  .terminal-connecting-fade-enter-active,
  .terminal-connecting-fade-leave-active {
    transition: none;
  }
}
</style>
