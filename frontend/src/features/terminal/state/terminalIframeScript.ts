import type { AgentType } from '@/types'

export function buildTerminalIframeHandlerScript(agentType?: AgentType): string {
  return `
      console.log('=== Claude Hub terminal handler injected ===');

      var CLAUDE_HUB_AGENT_TYPE = ${JSON.stringify(agentType || null)};

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
          '.xterm { width: 100% !important; height: 100% !important; box-sizing: border-box !important; margin: 0 !important; padding: 6px !important; background: ' + renderedBackground + ' !important; color: ' + page.foreground + ' !important; }' +
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
        if (CLAUDE_HUB_AGENT_TYPE !== 'codex' && CLAUDE_HUB_AGENT_TYPE !== 'traex' && CLAUDE_HUB_AGENT_TYPE !== 'claude' && CLAUDE_HUB_AGENT_TYPE !== 'cursor') return;

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

        // Structured composers submit an entire prompt as one ordered frame.
        // Sending every character through separate browser tasks can leave the
        // final carriage return behind the rendered text under load, making a
        // successful-looking Send stop at Claude's input box.
        if (event.data.type === 'terminal-text') {
          var text = typeof event.data.text === 'string' ? event.data.text : '';
          if (text && sendText(text)) {
            // Keep the history-replay input generation in sync with virtual
            // keyboard and SAB input without dispatching the text twice.
            try {
              window.postMessage({
                type: 'terminal-key',
                tabId: event.data.tabId || null,
                key: ''
              }, '*');
            } catch (_) { /* best-effort */ }
            return;
          }
          window.parent.postMessage({
            type: 'terminal-not-ready',
            tabId: event.data.tabId || null
          }, '*');
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
}
