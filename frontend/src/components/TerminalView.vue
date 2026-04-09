<template>
  <div class="terminal-container">
    <iframe
      v-if="tabId"
      :key="tabId"
      :ref="(el) => registerIframe(el, tabId)"
      :src="`/api/terminal/proxy/${tabId}/`"
      class="terminal-iframe"
      frameborder="0"
      allowfullscreen
      scrolling="yes"
      @load="onIframeLoad($event, tabId)"
    ></iframe>
  </div>
</template>

<script setup lang="ts">
import { onMounted } from 'vue'

const props = defineProps<{
  tabId: string
}>()

let iframeRefs: Record<string, HTMLIFrameElement | null> = {}

function registerIframe(el: any, tabId: string) {
  if (el) {
    iframeRefs[tabId] = el as HTMLIFrameElement
  }
}

function onIframeLoad(event: Event, tabId: string) {
  const iframe = event.target as HTMLIFrameElement
  if (!iframe || !iframe.contentDocument) return

  registerIframe(iframe, tabId)

  try {
    const script = iframe.contentDocument.createElement('script')
    script.textContent = `
      console.log('=== Minimal terminal handler injected ===');

      // Only prevent browser context menu, let everything else work normally
      document.addEventListener('contextmenu', function(e) {
        e.preventDefault();
        e.stopPropagation();
        return false;
      }, true);

      window.addEventListener('message', function(event) {
        console.log('Iframe received message:', event.data);
        if (!event.data || event.data.type !== 'terminal-key') return;

        const { key, ctrl, shift } = event.data;
        console.log('Processing key in iframe:', { key, ctrl, shift });

        let terminal = null;
        if (window.terminal) terminal = window.terminal;
        if (window.ttyd && window.ttyd.terminal) terminal = window.ttyd.terminal;
        if (window.ttyd && window.ttyd.term) terminal = window.ttyd.term;
        if (window.term) terminal = window.term;

        function sendText(text) {
          console.log('Trying to send text:', JSON.stringify(text));
          if (terminal && typeof terminal.send === 'function') {
            terminal.send(text);
            console.log('✓ Sent via terminal.send()');
            return true;
          }

          const textarea = document.querySelector('textarea');
          if (textarea) {
            console.log('Found textarea, sending events');
            textarea.focus();

            const keyCodeMap = {
              'Escape': 27, 'Tab': 9, 'Enter': 13,
              'ArrowUp': 38, 'ArrowDown': 40,
              'ArrowLeft': 37, 'ArrowRight': 39,
              'PageUp': 33, 'PageDown': 34,
            };
            const keyCode = keyCodeMap[key] || 0;

            const eventInit = {
              key,
              code: key,
              keyCode,
              which: keyCode,
              bubbles: true,
              cancelable: true,
              shiftKey: shift,
              ctrlKey: ctrl,
            };

            textarea.dispatchEvent(new KeyboardEvent('keydown', eventInit));
            textarea.dispatchEvent(new KeyboardEvent('keypress', eventInit));
            setTimeout(() => {
              textarea.dispatchEvent(new KeyboardEvent('keyup', eventInit));
            }, 10);
            return true;
          }

          console.log('✗ No method found to send key');
          return false;
        }

        let sent = false;
        if (key === 'Enter') sent = sendText('\\r');
        else if (key === 'Tab') sent = sendText('\\t');
        else if (key === 'Escape') sent = sendText('\\x1b');
        else if (key === 'ArrowUp') sent = sendText('\\x1b[A');
        else if (key === 'ArrowDown') sent = sendText('\\x1b[B');
        else if (key === 'ArrowRight') sent = sendText('\\x1b[C');
        else if (key === 'ArrowLeft') sent = sendText('\\x1b[D');
        else if (key === 'PageUp') sent = sendText('\\x1b[5~');
        else if (key === 'PageDown') sent = sendText('\\x1b[6~');

        if (sent) {
          console.log('✓ Key processing complete');
        } else {
          console.log('✗ Key not sent');
        }
      });

      console.log('=== Minimal terminal handler ready ===');
    `
    iframe.contentDocument.head.appendChild(script)
  } catch (e) {
    console.error('Error injecting script into iframe:', e)
  }
}

onMounted(() => {
  // Expose functions for MobileControls - use activePane's tabId
  if (typeof window !== 'undefined') {
    ;(window as any).__registerTerminalIframe = registerIframe
    ;(window as any).__sendTerminalKey = function(key: string, ctrl: boolean, shift: boolean) {
      console.log('Parent sending terminal key:', { key, ctrl, shift })

      // Try to get the active pane from the store via window
      const activePaneTabId = (window as any).__activePaneTabId
      const targetTabId = activePaneTabId || props.tabId

      const iframe = iframeRefs[targetTabId]
      if (iframe && iframe.contentWindow) {
        iframe.contentWindow.postMessage({
          type: 'terminal-key',
          key,
          ctrl,
          shift
        }, '*')
      } else {
        console.warn('No iframe found for tab:', targetTabId)
      }
    }
  }
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
}
</style>
