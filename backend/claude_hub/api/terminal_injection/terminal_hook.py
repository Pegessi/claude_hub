"""Terminal discovery and final hook fragment."""


def render_terminal_hook() -> str:
    return f"""        // Poll until both viewport object and DOM element are ready
        function tryEnable() {{
          if (enableNativeTouchScroll()) return;
          var tries = 0;
          var iv = setInterval(function() {{
            tries++;
            if (enableNativeTouchScroll() || tries > 100) clearInterval(iv);
          }}, 100);
        }}
        tryEnable();

        // ttyd uses Object.defineProperty(window, 'term', ...) internally,
        // and its bundled copy of Object.defineProperty was captured
        // before our script ran.  So we cannot intercept it via the
        // global.  Instead, poll for window.term being set and hook it
        // once it appears.  We also check immediately in case it was
        // already set.
        function tryHookTerm() {{
          if (!historyLoaded) {{
            return false;
          }}
          if (window.term && typeof window.term === 'object' && !window.term.__claudeHubHistoryHooked) {{
            currentTerm = window.term;
            hookTerm(window.term);

            // Belt-and-suspenders: when the iframe first loads, parent CSS
            // transitions (TabBar / pane-header max-height @ 180ms) can make
            // the container reach its final size after xterm's initial fit()
            // has already measured a smaller height and set rows too small.
            // The ResizeObserver in setupResizeGuard catches most cases,
            // but if observation begins after the element has already
            // reached its final size ResizeObserver will not fire until
            // the next size change. Schedule two delayed fits past the
            // longest parent transition to guarantee rows are correct.
            // On tabs where history replay takes time (agent tabs),
            // __claudeHubRequestFit is attached during setupResizeGuard
            // which runs synchronously inside hookTerm.
            function requestLateFit() {{
              const t = currentTerm || (window.term && typeof window.term === 'object' ? window.term : null);
              if (t && typeof t.__claudeHubRequestFit === 'function') {{
    t.__claudeHubRequestFit();
              }}
            }}
            setTimeout(requestLateFit, 250);
            setTimeout(requestLateFit, 500);

            // Notify parent that terminal is ready for key input
            if (window.parent && window.parent !== window) {{
              window.parent.postMessage({{
    type: 'terminal-ready',
    tabId: TAB_ID
              }}, '*');
            }}
            return true;
          }}
          return false;
        }}

        // Try immediately (ttyd may have already set it)
        if (!tryHookTerm()) {{
          // Not set yet — poll until it appears (ttyd sets it during init)
          const pollTimer = setInterval(function() {{
            if (tryHookTerm()) {{
              clearInterval(pollTimer);
            }}
          }}, 50);
          // Safety: stop polling after 10 seconds
          setTimeout(function() {{ clearInterval(pollTimer); }}, 10000);
        }}
      }})();
    </script>
"""
