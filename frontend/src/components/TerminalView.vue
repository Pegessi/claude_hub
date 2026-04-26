<template>
  <div class="terminal-container">
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
  </div>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from 'vue'

const props = defineProps<{
  tabId: string
}>()

let iframeRefs: Record<string, HTMLIFrameElement | null> = {}
const cachedTabIds = ref<string[]>([])

// Track which terminals are ready (have a valid terminal.send())
const terminalReady: Record<string, boolean> = {}

// Key queue per tab — holds keys that were pressed before terminal was ready
const keyQueues: Record<string, Array<{ key: string; ctrl: boolean; shift: boolean }>> = {}

function cacheTabId(tabId: string) {
  if (!tabId) return
  if (!cachedTabIds.value.includes(tabId)) {
    cachedTabIds.value.push(tabId)
  }
}

watch(
  () => props.tabId,
  (newTabId) => {
    cacheTabId(newTabId)
  },
  { immediate: true }
)

function registerIframe(el: any, tabId: string) {
  if (el instanceof HTMLIFrameElement) {
    iframeRefs[tabId] = el as HTMLIFrameElement
  } else {
    delete iframeRefs[tabId]
  }
}

function flushKeyQueue(tabId: string) {
  const queue = keyQueues[tabId]
  if (!queue || queue.length === 0) return

  const iframe = iframeRefs[tabId]
  if (!iframe || !iframe.contentWindow) return

  for (const item of queue) {
    iframe.contentWindow.postMessage({
      type: 'terminal-key',
      key: item.key,
      ctrl: item.ctrl,
      shift: item.shift
    }, '*')
  }
  queue.length = 0
}

function onIframeLoad(event: Event, tabId: string) {
  const iframe = event.target as HTMLIFrameElement
  if (!iframe || !iframe.contentDocument) return

  registerIframe(iframe, tabId)

  try {
    const script = iframe.contentDocument.createElement('script')
    script.textContent = `
      console.log('=== Claude Hub terminal handler injected ===');

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

      // Send text to terminal via terminal.send() — the reliable path
      function sendText(text) {
        var term = findTerminal();
        if (term && typeof term.send === 'function') {
          try {
            term.send(text);
            return true;
          } catch(e) {
            console.warn('terminal.send() failed:', e);
            return false;
          }
        }
        console.warn('No terminal.send() available');
        return false;
      }

      // Signal parent when terminal is ready
      function notifyReady() {
        if (window.parent && window.parent !== window) {
          var tabId = null;
          // Extract tabId from URL: /api/terminal/proxy/{tabId}/
          var match = window.location.pathname.match(/\\/proxy\\/([^/]+)\\//);
          if (match) tabId = match[1];
          window.parent.postMessage({
            type: 'terminal-ready',
            tabId: tabId
          }, '*');
        }
      }

      // Watch for terminal becoming available (ttyd sets it asynchronously)
      var termCheckInterval = setInterval(function() {
        var term = findTerminal();
        if (term && typeof term.send === 'function') {
          clearInterval(termCheckInterval);
          console.log('=== Terminal ready, notifying parent ===');
          notifyReady();
        }
      }, 100);

      // Safety: stop checking after 15 seconds
      setTimeout(function() { clearInterval(termCheckInterval); }, 15000);

      // Handle key messages from parent (virtual keyboard)
      window.addEventListener('message', function(event) {
        if (!event.data || event.data.type !== 'terminal-key') return;

        var key = event.data.key;
        var ctrl = event.data.ctrl || false;
        var shift = event.data.shift || false;

        var sent = false;

        // Ctrl + letter: send control character
        // Ctrl+A=\x01, Ctrl+B=\x02, ..., Ctrl+Z=\x1a
        if (ctrl && key.length === 1) {
          var code = key.toUpperCase().charCodeAt(0) - 64;
          if (code >= 1 && code <= 26) {
            sent = sendText(String.fromCharCode(code));
          }
        }

        // Shift + Tab: \x1b[Z
        if (!sent && shift && key === 'Tab') {
          sent = sendText('\\x1b[Z');
        }

        // Standard key mappings
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

        // If send failed, notify parent that terminal is not ready
        // so it can re-queue the key
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
  } catch (e) {
    console.error('Error injecting script into iframe:', e)
  }
}

// Listen for messages from iframes
function handleMessage(event: MessageEvent) {
  if (!event.data) return

  if (event.data.type === 'terminal-ready') {
    const tabId = event.data.tabId
    if (tabId) {
      terminalReady[tabId] = true
      // Flush any queued keys for this tab
      flushKeyQueue(tabId)
    }
  }

  if (event.data.type === 'terminal-not-ready') {
    const tabId = event.data.tabId
    if (tabId) {
      terminalReady[tabId] = false
    }
  }

  // Handle terminal-click for pane activation
  if (event.data.type === 'terminal-click') {
    // This is handled by TerminalPane.vue
  }
}

onMounted(() => {
  if (typeof window !== 'undefined') {
    ;(window as any).__registerTerminalIframe = registerIframe

    // Key sending function with queue support
    ;(window as any).__sendTerminalKey = function(key: string, ctrl: boolean, shift: boolean) {
      const activePaneTabId = (window as any).__activePaneTabId
      const targetTabId = activePaneTabId || props.tabId

      const iframe = iframeRefs[targetTabId]
      if (iframe && iframe.contentWindow) {
        if (terminalReady[targetTabId]) {
          // Terminal is ready — send directly
          iframe.contentWindow.postMessage({
            type: 'terminal-key',
            key,
            ctrl,
            shift,
            tabId: targetTabId
          }, '*')
        } else {
          // Terminal not ready — queue the key
          if (!keyQueues[targetTabId]) {
            keyQueues[targetTabId] = []
          }
          keyQueues[targetTabId].push({ key, ctrl, shift })
        }
      } else {
        console.warn('No iframe found for tab:', targetTabId)
        // Queue for when iframe appears
        if (!keyQueues[targetTabId]) {
          keyQueues[targetTabId] = []
        }
        keyQueues[targetTabId].push({ key, ctrl, shift })
      }
    }

    window.addEventListener('message', handleMessage)
  }
})

onUnmounted(() => {
  window.removeEventListener('message', handleMessage)
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
</style>
