"""Terminal hook and resize bootstrap fragment."""


def render_terminal_setup() -> str:
    return f"""        function hookTerm(term) {{
          if (!term || term.__claudeHubHistoryHooked || typeof term.open !== 'function') return;
          term.__claudeHubHistoryHooked = true;
          // If term.open() was already called (element is attached to DOM),
          // ttyd has already written the visible screen content.  We must
          // do a full replay (clear + rewrite everything) and discard ttyd's
          // buffered WS data (which would duplicate the visible screen).
          // Otherwise, hook term.open so replay runs right after ttyd
          // calls it — only scrollback is written, ttyd WS fills the screen.
          if (term.element) {{
            replayHistory(term, true);
            setupResizeGuard(term);
          }} else {{
            const originalOpen = term.open.bind(term);
            term.open = function(...args) {{
              const result = originalOpen(...args);
              // xterm.js behavior differs across browser/platform versions
              // when large writes happen immediately after open(): in
              // Ubuntu CI, the Phase A scroll-up strategy can leave only
              // the visible screen in the buffer. Full replay is more
              // deterministic: clear after open(), write tmux's complete
              // captured content, and discard ttyd's duplicate initial WS
              // payload while replay is in progress.
              replayHistory(term, true);
              setupResizeGuard(term);
              return result;
            }};
          }}
        }}

        // ---- Mobile keyboard resize debounce ----
        // When the virtual keyboard appears/disappears on mobile,
        // the viewport shrinks/expands, causing xterm.js to resize
        // repeatedly. With large scrollback this triggers a feedback
        // loop: resize -> tmux redraw -> xterm re-render -> layout shift
        // -> another resize. We break this loop by debouncing resize
        // events and using visualViewport API to detect genuine
        // keyboard state changes vs transient layout jank.
        const DEBOUNCE_MS = 150;

        function setupResizeGuard(term) {{
          if (!term || term.__claudeHubResizeGuarded) return;
          term.__claudeHubResizeGuarded = true;

          // Debounce ttyd's resize-to-tmux path: xterm.js onResize
          // callback is intercepted so only the final stable dimension
          // is forwarded to tmux, preventing intermediate resizes from
          // triggering expensive redraws on large scrollback buffers.
          if (typeof term.onResize === 'function') {{
            const origOnResize = term.onResize;
            let lastArgs = null;
            let pending = false;
            let timer = null;

            term.onResize = function(cols, rows) {{
              lastArgs = [cols, rows];
              if (pending) return;
              pending = true;
              timer = setTimeout(function() {{
    pending = false;
    if (lastArgs) origOnResize.apply(term, lastArgs);
    lastArgs = null;
    // Layout changed without a scroll event: refresh the
    // resync closure's cached "at bottom" flag if available.
    if (typeof term.__claudeHubRecomputeBottom === 'function') {{
      term.__claudeHubRecomputeBottom();
    }}
              }}, DEBOUNCE_MS);
            }};
          }}

          // Direct container observation inside the iframe.
          //
          // Historically we relied on the parent frame's ResizeObserver to
          // post a 'terminal-resize' message whenever the pane size changed,
          // which dispatched window.resize inside the iframe and ttyd's
          // FitAddon re-measured. That chain is fragile: (a) if the message
          // arrives before the iframe terminal is ready it is dropped,
          // (b) during initial mount / tab switch / CSS transitions the
          // parent container can reach its final size between two parent
          // rAF ticks with no subsequent ResizeObserver fire, leaving
          // xterm fit() with stale too-small rows and the bottom input
          // line painted off-screen. Observing the terminal host directly
          // from inside the iframe — same frame xterm lives in — removes
          // the cross-context timing dependency: any size change of the
          // element xterm renders into (body, since ttyd sizes html/body
          // to 100% of the iframe) triggers a debounced fit().
          function callFit() {{
            try {{
              if (term.fitAddon && typeof term.fitAddon.fit === 'function') {{
    term.fitAddon.fit();
              }} else if (typeof term.fit === 'function') {{
    term.fit();
              }}
              if (typeof term.__claudeHubRecomputeBottom === 'function') {{
    term.__claudeHubRecomputeBottom();
              }}
            }} catch (e) {{}}
          }}
          // Expose on term so other closures (history refresh / replay)
          // can request a re-fit without duplicating the fallback chain.
          term.__claudeHubRequestFit = callFit;

          let fitTimer = null;
          function scheduleFit() {{
            if (fitTimer !== null) clearTimeout(fitTimer);
            fitTimer = setTimeout(function() {{
              fitTimer = null;
              callFit();
              // A second fit one frame later catches the case where the
              // first fit happens in the same frame as a CSS transition
              // update — e.g. the pane-header max-height transition settling
              // at 180ms shifts body height by ~28px, and the first fit may
              // read pre-layout dimensions.
              if (typeof requestAnimationFrame === 'function') {{
    requestAnimationFrame(function() {{ callFit(); }});
              }}
            }}, DEBOUNCE_MS);
          }}

          // Observe body (covers html/body {{width:100%;height:100%}}) and,
          // if present, the xterm container element. ResizeObserver is
          // available in all evergreen desktop browsers (Chrome 64+,
          // Firefox 69+, Safari 13.1+); fall back to a window.resize
          // listener where it isn't.
          if (typeof ResizeObserver !== 'undefined') {{
            const resizeObserver = new ResizeObserver(function(entries) {{
              // Ignore fired-before-layout observations of 0-sized
              // elements — fitting to 0 rows/cols confuses tmux.
              for (let i = 0; i < entries.length; i++) {{
    const rect = entries[i] && entries[i].contentRect;
    if (rect && (rect.width > 8) && (rect.height > 8)) {{
      scheduleFit();
      return;
    }}
              }}
            }});
            try {{ resizeObserver.observe(document.body); }} catch (e) {{}}
            const termEl = document.getElementById('terminal')
              || document.querySelector('.terminal')
              || (term.element && term.element.parentElement);
            if (termEl) {{
              try {{ resizeObserver.observe(termEl); }} catch (e) {{}}
            }}
            term.__claudeHubResizeObserver = resizeObserver;
          }} else {{
            window.addEventListener('resize', scheduleFit);
          }}

          // On mobile, visualViewport.resize fires many times as the
          // keyboard animates in/out. We only act when the keyboard
          // reaches a stable state (visible or hidden), and ignore
          // all intermediate transitions.
          if (window.visualViewport) {{
            let keyboardVisible = false;
            let vvTimer = null;

            window.visualViewport.addEventListener('resize', function() {{
              clearTimeout(vvTimer);
              vvTimer = setTimeout(function() {{
    const vv = window.visualViewport;
    const nowKeyboard = (vv.height < window.innerHeight * 0.8);

    if (nowKeyboard !== keyboardVisible) {{
      keyboardVisible = nowKeyboard;
      // Trigger a single, stable fit after keyboard
      // animation is complete
      callFit();
    }}
    // Keyboard state unchanged = transient jank, ignore
              }}, DEBOUNCE_MS);
            }});
          }}
        }}

        // ---- History resync after live-output bursts ----
        // ttyd is attached to tmux as a terminal client, so under fast
        // wrapped output tmux can optimize what it sends to the client and
        // skip scrollback that is still present in tmux history. After live
        // output goes idle and the user is at the bottom, reconcile xterm
        // with tmux history so newly produced long/wrapped output is complete.
        // Agent TUIs such as Claude/Codex redraw status panes with relative
        // cursor operations; replaying a plain tmux snapshot mid-redraw breaks
        // that cursor state and leaves stale/status fragments on screen.
        const AUTO_HISTORY_REPLAY_ENABLED = AGENT_TYPE === 'terminal';
        const AUTO_HISTORY_RESYNC_ENABLED = AUTO_HISTORY_REPLAY_ENABLED;
        const PROTECT_AGENT_HISTORY_VIEW = IS_AGENT_TUI;
        const RESYNC_IDLE_MS = 700;
        const RESYNC_MIN_PRINTABLE_CHARS = 4096;
        const RESYNC_MIN_LINE_BREAKS = 8;

"""
