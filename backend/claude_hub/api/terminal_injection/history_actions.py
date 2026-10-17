"""External history action handlers fragment."""


def render_history_actions() -> str:
    return f"""        function termForHistoryAction() {{
          return currentTerm || _getTerm();
        }}

        function refreshHistoryWhenReady(options, attemptsLeft) {{
          const opts = options || {{}};
          const reason = opts.reason || 'manual';
          const requestId = opts.requestId || null;
          const term = termForHistoryAction();

          if (term && typeof term.__claudeHubRefreshHistory === 'function') {{
            Promise.resolve(term.__claudeHubRefreshHistory(opts))
              .then(function(ok) {{
    postHistoryRefreshResult(reason, ok !== false, null, requestId);
              }})
              .catch(function(error) {{
    console.debug('claude-hub history refresh failed', error);
    postHistoryRefreshResult(reason, false, error && error.message ? error.message : 'refresh failed', requestId);
              }});
            return;
          }}

          if (attemptsLeft > 0) {{
            setTimeout(function() {{
              refreshHistoryWhenReady(opts, attemptsLeft - 1);
            }}, 100);
            return;
          }}

          postHistoryRefreshResult(reason, false, 'terminal not ready', requestId);
        }}

        function terminalIsAtBottom(term) {{
          try {{
            const buf = term && term.buffer && term.buffer.active;
            if (buf && Number.isInteger(buf.viewportY) && Number.isInteger(buf.baseY)) {{
              if (buf.viewportY < buf.baseY) return false;
            }}
            const vp = document.querySelector('.xterm-viewport');
            if (vp) {{
              // Treat "within 2px of the bottom" as at-bottom (sub-pixel rounding).
              if (vp.scrollHeight - vp.clientHeight - vp.scrollTop > 2) return false;
            }}
            return true;
          }} catch (error) {{
            return true;
          }}
        }}

        // Robust scroll-to-bottom for tab activation / switch.
        //
        // The old implementation issued a single scroll the moment `term`
        // existed. On tab switch that races with (a) the term not having
        // finished its initial history replay and (b) the renderer not having
        // painted, so the one-shot scroll could land mid-history and stick —
        // the reported "switching to a new terminal shows history, not the
        // bottom prompt" bug. Instead: wait until any in-flight replay settles
        // (bounded), scroll, then verify we actually reached the bottom AND the
        // scrollHeight is stable between two ticks, retrying briefly otherwise.
        // This never reintroduces a history replay — it is scroll-only.
        function scrollBottomWhenReady(attemptsLeft, settleTries, lastScrollHeight) {{
          const term = termForHistoryAction();
          if (!term) {{
            if (attemptsLeft > 0) {{
              setTimeout(function() {{
    scrollBottomWhenReady(attemptsLeft - 1, settleTries, lastScrollHeight);
              }}, 100);
            }}
            return;
          }}

          // Ready check: do not scroll while the initial replay is still
          // buffering/writing, or the buffer will be rewritten under us.
          if (term.__claudeHubReplayBuffering === true && attemptsLeft > 0) {{
            setTimeout(function() {{
              scrollBottomWhenReady(attemptsLeft - 1, settleTries, lastScrollHeight);
            }}, 100);
            return;
          }}

          if (typeof term.__claudeHubScrollToBottom === 'function') {{
            term.__claudeHubScrollToBottom();
          }} else {{
            scrollTerminalToBottom(term);
          }}

          const vp = document.querySelector('.xterm-viewport');
          const scrollHeight = vp ? vp.scrollHeight : 0;
          const remainingSettle = (typeof settleTries === 'number') ? settleTries : 12;
          // Settled = visually at the bottom AND content height stable across
          // two consecutive checks (renderer/replay no longer growing it).
          const settled = terminalIsAtBottom(term) && scrollHeight === lastScrollHeight;
          if (remainingSettle > 0 && !settled) {{
            setTimeout(function() {{
              scrollBottomWhenReady(0, remainingSettle - 1, scrollHeight);
            }}, 60);
          }}
        }}

        window.addEventListener('message', function(event) {{
          if (!event.data || (event.data.tabId && event.data.tabId !== TAB_ID)) return;

          if (event.data.type === 'terminal-key') {{
            noteTerminalUserInput();
            return;
          }}

          if (event.data.type === 'terminal-bootstrap-correlation') {{
            storeBootstrapCorrelation(
              event.data.requestId || null,
              event.data.agentStatus || null,
            );
            return;
          }}

          if (event.data.type === 'terminal-history-refresh') {{
            refreshHistoryWhenReady({{
              reason: event.data.reason || 'manual',
              scrollToBottom: event.data.scrollToBottom !== false,
              preserveUserScroll: event.data.preserveUserScroll === true,
              requestId: event.data.requestId || null,
            }}, 80);
            return;
          }}

          if (event.data.type === 'terminal-scroll-bottom') {{
            scrollBottomWhenReady(50);
            return;
          }}

          if (event.data.type === 'terminal-select-mode') {{
            setSelectMode(!!event.data.enabled);
            return;
          }}

          if (event.data.type === 'terminal-activate') {{
            if (event.data.refreshHistory) {{
              refreshHistoryWhenReady({{
    reason: 'activate',
    scrollToBottom: event.data.scrollToBottom !== false,
              }}, 80);
            }} else if (event.data.scrollToBottom) {{
              scrollBottomWhenReady(50);
            }}
          }}
        }});

        window.addEventListener('keydown', noteTerminalUserInput, true);
        window.addEventListener('beforeinput', noteTerminalUserInput, true);
        window.addEventListener('paste', noteTerminalUserInput, true);

"""
