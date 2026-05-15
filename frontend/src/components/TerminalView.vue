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
import { storeToRefs } from 'pinia'
import { useAppStore } from '@/stores/appStore'
import type { AgentType } from '@/types'

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

declare global {
  interface Window {
    __activePaneTabId?: string | null
    __claudeHubTerminalState?: TerminalKeyState
    __registerTerminalIframe?: (el: HTMLIFrameElement | null, tabId: string) => void
    __sendTerminalKey?: (key: string, ctrl?: boolean, shift?: boolean) => void
  }
}

const iframeRefs: Record<string, HTMLIFrameElement | null> = {}
const cachedTabIds = ref<string[]>([])
const appStore = useAppStore()
const { colorScheme } = storeToRefs(appStore)

function getTerminalState(): TerminalKeyState {
  if (!window.__claudeHubTerminalState) {
    window.__claudeHubTerminalState = {
      iframes: {},
      ready: {},
      queues: {},
    }
  }
  return window.__claudeHubTerminalState
}

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
  }
}

function postTerminalKey(tabId: string, item: TerminalKeyItem): boolean {
  const state = getTerminalState()
  const iframe = state.iframes[tabId]
  if (!iframe || !iframe.contentWindow) return false

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
    minimumContrastRatio: colorScheme.value === 'light' ? 4.5 : 1,
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
      cursorAccent: background,
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

function postTerminalTheme(tabId?: string) {
  const payload = terminalThemePayload()
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

function onIframeLoad(event: Event, tabId: string) {
  const iframe = event.target as HTMLIFrameElement
  if (!iframe || !iframe.contentDocument) return

  registerIframe(iframe, tabId)

  try {
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

      function ensureTerminalThemeStyle() {
        var style = document.getElementById('claude-hub-terminal-theme');
        if (!style) {
          style = document.createElement('style');
          style.id = 'claude-hub-terminal-theme';
          document.head.appendChild(style);
        }
        return style;
      }

      function applyTerminalTheme(payload) {
        if (!payload || !payload.xterm || !payload.page) return;
        pendingTerminalTheme = payload;

        var page = payload.page;
        document.documentElement.dataset.theme = payload.scheme || 'dark';
        document.documentElement.style.backgroundColor = page.background;
        document.body.style.backgroundColor = page.background;
        document.body.style.color = page.foreground;

        ensureTerminalThemeStyle().textContent =
          'html, body, #terminal, .terminal, .xterm { background: ' + page.background + ' !important; color: ' + page.foreground + ' !important; }' +
          '.xterm-viewport { background-color: ' + page.background + ' !important; }' +
          '.xterm-screen canvas { filter: ' + page.canvasFilter + ' !important; }' +
          '.xterm-selection div { background-color: ' + page.selection + ' !important; }';

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
      // Claude Code and Codex handle Ctrl+V by reading the macOS clipboard, so
      // first sync the browser image data to the backend pasteboard and then
      // trigger that key.
      document.addEventListener('paste', function(event) {
        if (CLAUDE_HUB_AGENT_TYPE !== 'codex' && CLAUDE_HUB_AGENT_TYPE !== 'claude') return;

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
        if (hasTerminalInputApi()) {
          clearInterval(termCheckInterval);
          if (pendingTerminalTheme) applyTerminalTheme(pendingTerminalTheme);
          console.log('=== Terminal ready, notifying parent ===');
          notifyReady();
        }
      }, 100);

      // Safety: stop checking after 15 seconds
      setTimeout(function() { clearInterval(termCheckInterval); }, 15000);

      // Handle key messages from parent (virtual keyboard)
      window.addEventListener('message', function(event) {
        if (!event.data) return;

        if (event.data.type === 'terminal-theme') {
          applyTerminalTheme(event.data.payload);
          return;
        }

        if (event.data.type !== 'terminal-key') return;

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
    postTerminalTheme(tabId)
  } catch (e) {
    console.error('Error injecting script into iframe:', e)
  }
}

watch(colorScheme, () => {
  requestAnimationFrame(() => postTerminalTheme())
})

// Listen for messages from iframes
function handleMessage(event: MessageEvent) {
  if (!event.data) return

  if (event.data.type === 'terminal-ready') {
    const tabId = event.data.tabId
    if (tabId) {
      getTerminalState().ready[tabId] = true
      // Flush any queued keys for this tab
      flushKeyQueue(tabId)
    }
  }

  if (event.data.type === 'terminal-not-ready') {
    const tabId = event.data.tabId
    if (tabId) {
      getTerminalState().ready[tabId] = false
    }
  }

  // Handle terminal-click for pane activation
  if (event.data.type === 'terminal-click') {
    // This is handled by TerminalPane.vue
  }
}

onMounted(() => {
  if (typeof window !== 'undefined') {
    window.__registerTerminalIframe = registerIframe

    // Key sending function with queue support
    window.__sendTerminalKey = function(key: string, ctrl = false, shift = false) {
      const activePaneTabId = window.__activePaneTabId
      const targetTabId = activePaneTabId || props.tabId
      if (!targetTabId) return

      const item = { key, ctrl, shift }
      const state = getTerminalState()
      if (state.ready[targetTabId] && postTerminalKey(targetTabId, item)) {
        return
      }

      if (state.iframes[targetTabId]) {
        // Terminal exists but is not ready yet.
        queueTerminalKey(targetTabId, item)
      } else {
        console.warn('No iframe found for tab:', targetTabId)
        // Queue for when iframe appears
        queueTerminalKey(targetTabId, item)
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
