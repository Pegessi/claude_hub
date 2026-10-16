"""Runtime half of the injected terminal history script."""


def render_terminal_runtime() -> str:
    return f"""        function setupHistoryResync(term) {{
          if (!term || term.__claudeHubHistoryResyncHooked || typeof term.write !== 'function') return;
          term.__claudeHubHistoryResyncHooked = true;

          const innerWriteRef = term.write;
          function writeThrough(data, cb) {{
            return innerWriteRef.call(term, data, cb);
          }}
          term.__claudeHubInnerWrite = writeThrough;
          let writeGeneration = 0;
          let timer = null;
          let resyncing = false;
          let pendingWhenBottom = false;
          let bottomFollowQueued = false;
          let bottomFollowGeneration = 0;
          let bottomFollowUntil = 0;
          const resyncBuffer = [];
          let forcedRefreshQueue = [];
          let forcedRefreshPumpPromise = null;
          let historySyncOwner = 0;
          let agentHistoryViewNeedsSnapshot = false;
          let pendingResyncChars = 0;
          let pendingResyncLineBreaks = 0;
          let inputQuietResyncTimer = null;
          let nextAutoResyncId = 1;
          let activeAutoResyncId = null;
          const cancelledAutoResyncIds = new Set();
          // Hot-path optimization: cache the viewport element and an event-driven
          // "at bottom" flag so per-frame term.write does not force a synchronous
          // layout reflow (querySelector + scrollTop/scrollHeight/clientHeight).
          let cachedViewportEl = null;
          let domAtBottomCached = true;

          function viewportElement() {{
            if (!cachedViewportEl || !cachedViewportEl.isConnected) {{
              cachedViewportEl = document.querySelector('.xterm-viewport');
            }}
            return cachedViewportEl;
          }}

          function recomputeDomAtBottom() {{
            const el = viewportElement();
            domAtBottomCached = !el ||
              el.scrollTop >= el.scrollHeight - el.clientHeight - 1;
            return domAtBottomCached;
          }}

          // Expose the recompute so the (sibling-scope) resize guard can
          // refresh the cached flag after a layout change that alters
          // scrollHeight/clientHeight without firing a scroll event
          // (window/pane resize, mobile keyboard fit()). The DOM geometry
          // read forces a synchronous reflow, so calling this immediately
          // after fit() reads the post-resize layout.
          term.__claudeHubRecomputeBottom = recomputeDomAtBottom;

          function bufferService() {{
            return term._core && term._core._bufferService;
          }}

          function isAtBottom() {{
            const buffer = term.buffer && term.buffer.active;
            const service = bufferService();
            if (!buffer) return true;
            return !(service && service.isUserScrolling) && viewportIsAtBottom();
          }}

          function viewportIsAtBottom() {{
            const buffer = term.buffer && term.buffer.active;
            if (!buffer) return true;
            return buffer.viewportY === buffer.baseY && domAtBottomCached;
          }}

          function terminalDataStats(data) {{
            // Runs on EVERY output frame via noteLiveWrite -> noteResyncPressure,
            // so it must stay allocation-free and O(n) with a tiny constant. A
            // raw byte/char scan replaces the old TextDecoder + 4 regex passes,
            // which dominated the main thread under heavy output and starved
            // keystroke echo (see docs/working-logs/2026-06-14-terminal-input-latency-v4.md).
            // The only consumers are the coarse burst thresholds in
            // hasEnoughResyncPressure() (chars >= 4096 OR lineBreaks >= 8), so an
            // approximate `chars` (raw length, incl. ANSI/multibyte) is fine; it
            // merely arms the idle resync marginally earlier. `lineBreaks` stays
            // exact because 0x0a is single-byte and never a UTF-8 continuation.
            if (data instanceof Uint8Array) {{
              let lineBreaks = 0;
              for (let i = 0; i < data.length; i++) {{
    if (data[i] === 10) lineBreaks++;
              }}
              return {{ chars: data.length, lineBreaks: lineBreaks }};
            }}
            const text = String(data || '');
            let lineBreaks = 0;
            for (let i = 0; i < text.length; i++) {{
              if (text.charCodeAt(i) === 10) lineBreaks++;
            }}
            return {{ chars: text.length, lineBreaks: lineBreaks }};
          }}

          function noteResyncPressure(data) {{
            if (!AUTO_HISTORY_RESYNC_ENABLED) return;
            const stats = terminalDataStats(data);
            pendingResyncChars += stats.chars;
            pendingResyncLineBreaks += stats.lineBreaks;
          }}

          function clearInputQuietResyncTimer() {{
            if (inputQuietResyncTimer) {{
              clearTimeout(inputQuietResyncTimer);
              inputQuietResyncTimer = null;
            }}
          }}

          function resetResyncPressure() {{
            pendingResyncChars = 0;
            pendingResyncLineBreaks = 0;
            clearInputQuietResyncTimer();
          }}

          function hasEnoughResyncPressure() {{
            return (
              pendingResyncChars >= RESYNC_MIN_PRINTABLE_CHARS ||
              pendingResyncLineBreaks >= RESYNC_MIN_LINE_BREAKS
            );
          }}

          function scheduleResyncAfterInputQuiet() {{
            if (!AUTO_HISTORY_RESYNC_ENABLED || !hasEnoughResyncPressure()) return;
            clearInputQuietResyncTimer();
            inputQuietResyncTimer = setTimeout(function() {{
              inputQuietResyncTimer = null;
              scheduleResync(false);
            }}, userInputQuietDelayMs() + RESYNC_IDLE_MS);
          }}

          function cancelActiveAutoResyncForInput() {{
            if (activeAutoResyncId === null) return false;
            cancelledAutoResyncIds.add(activeAutoResyncId);
            activeAutoResyncId = null;
            if (resyncing) {{
              resyncing = false;
              flushResyncBuffer();
            }}
            return true;
          }}

          function deferScheduledResyncForInput() {{
            if (!AUTO_HISTORY_RESYNC_ENABLED) return;
            const cancelledActiveResync = cancelActiveAutoResyncForInput();
            if (!cancelledActiveResync && !hasEnoughResyncPressure()) return;
            if (timer) {{
              clearTimeout(timer);
              timer = null;
            }}
            if (hasEnoughResyncPressure()) {{
              scheduleResyncAfterInputQuiet();
            }}
          }}

          terminalUserInputListeners.push(deferScheduledResyncForInput);

          function scheduleResync(force) {{
            if (!AUTO_HISTORY_RESYNC_ENABLED) return;
            if (!force && !hasEnoughResyncPressure()) return;
            if (!force && hasRecentUserInput()) {{
              scheduleResyncAfterInputQuiet();
              return;
            }}
            if (resyncing) return;
            clearInputQuietResyncTimer();
            if (timer) clearTimeout(timer);
            timer = setTimeout(runResync, RESYNC_IDLE_MS);
          }}

          function scheduleBottomFollow(generationAtWrite, extendWindow) {{
            bottomFollowGeneration = generationAtWrite;
            if (extendWindow !== false) {{
              bottomFollowUntil = Math.max(bottomFollowUntil, Date.now() + 120);
            }}
            if (bottomFollowQueued) return;
            bottomFollowQueued = true;

            function needsBottomScroll() {{
              const buffer = term.buffer && term.buffer.active;
              if (!buffer) return false;
              return buffer.viewportY !== buffer.baseY || !domAtBottomCached;
            }}

            function run() {{
              bottomFollowQueued = false;
              if (userScrollGeneration !== bottomFollowGeneration) return;
              if (needsBottomScroll()) {{
    scrollTerminalToBottom(term, {{ refresh: false }});
    domAtBottomCached = true;
              }}
              if (Date.now() < bottomFollowUntil) {{
    scheduleBottomFollow(generationAtWrite, false);
              }}
            }}

            if (typeof requestAnimationFrame === 'function') {{
              requestAnimationFrame(run);
            }} else {{
              setTimeout(run, 0);
            }}
          }}

          function beginHistorySyncOwner() {{
            historySyncOwner++;
            if (activeAutoResyncId !== null) {{
              cancelledAutoResyncIds.add(activeAutoResyncId);
              activeAutoResyncId = null;
            }}
            if (timer) {{
              clearTimeout(timer);
              timer = null;
            }}
            return historySyncOwner;
          }}

          function isHistorySyncOwner(owner) {{
            return historySyncOwner === owner;
          }}

          function cancelAutoResyncForForcedRefresh() {{
            if (activeAutoResyncId !== null) {{
              cancelledAutoResyncIds.add(activeAutoResyncId);
              activeAutoResyncId = null;
            }}
            if (timer) {{
              clearTimeout(timer);
              timer = null;
            }}
            if (resyncing) {{
              resyncing = false;
              flushResyncBuffer();
            }}
          }}

          function flushResyncBuffer() {{
            flushResyncBufferThen(function() {{}});
          }}

          function flushResyncBufferThen(callback) {{
            function drainBatch() {{
              if (resyncBuffer.length === 0) {{
    if (typeof callback === 'function') callback();
    return;
              }}
              let pending = resyncBuffer.length;
              const batch = resyncBuffer.splice(0, resyncBuffer.length);
              batch.forEach(function(item) {{
    writeThrough(item.data, function() {{
      try {{
        if (typeof item.cb === 'function') item.cb();
      }} finally {{
        pending--;
        if (pending === 0) drainBatch();
      }}
    }});
              }});
            }}
            drainBatch();
          }}

          function applyHistoryPayloadWithOptions(term, payload, options, wt, cb) {{
            const shouldScrollToBottom = !options || options.scrollToBottom !== false;
            let savedViewportY = null;
            let savedScrollTop = null;
            if (!shouldScrollToBottom) {{
              try {{
    const buf = term.buffer && term.buffer.active;
    if (buf && buf.viewportY != null) savedViewportY = buf.viewportY;
    const vp = document.querySelector('.xterm-viewport');
    if (vp) savedScrollTop = vp.scrollTop;
              }} catch (e) {{}}
            }}

            function finishApply(ok) {{
              if (shouldScrollToBottom) {{
    scrollTerminalToBottom(term, {{ refresh: true }});
    domAtBottomCached = true;
              }}
              if (typeof term.__claudeHubRequestFit === 'function') {{
    if (!shouldScrollToBottom) {{
      const bs = term._core && term._core._bufferService;
      if (bs) bs.isUserScrolling = true;
      term.__claudeHubRequestFit();
    }} else if (typeof requestAnimationFrame === 'function') {{
      requestAnimationFrame(function() {{ term.__claudeHubRequestFit(); }});
    }} else {{
      setTimeout(function() {{ term.__claudeHubRequestFit(); }}, 16);
    }}
              }}
              if (!shouldScrollToBottom && (savedViewportY !== null || savedScrollTop !== null)) {{
    const bs = term._core && term._core._bufferService;
    if (bs) bs.isUserScrolling = true;
    if (savedViewportY !== null && typeof term.scrollToLine === 'function') {{
      term.scrollToLine(savedViewportY);
    }}
    const vp = document.querySelector('.xterm-viewport');
    if (vp && savedScrollTop !== null) vp.scrollTop = savedScrollTop;
              }}
              if (typeof cb === 'function') cb(ok);
            }}

            if (!payload) {{
              finishApply(true);
              return;
            }}

            wt(payload, function() {{
              finishApply(true);
            }});
          }}

          function scheduleForcedRefreshPump() {{
            if (forcedRefreshPumpPromise) return;
            forcedRefreshPumpPromise = (async function() {{
              try {{
    while (forcedRefreshQueue.length > 0) {{
      const entry = forcedRefreshQueue.shift();
      cancelAutoResyncForForcedRefresh();
      beginHistorySyncOwner();
      try {{
        const result = await executeForcedRefresh(entry.opts);
        entry.resolve(result);
      }} catch (error) {{
        if (resyncing) {{
          resyncing = false;
          flushResyncBuffer();
        }}
        entry.reject(error);
      }}
    }}
              }} finally {{
    forcedRefreshPumpPromise = null;
    if (forcedRefreshQueue.length > 0) {{
      scheduleForcedRefreshPump();
    }}
              }}
            }})();
          }}

          function enqueueForcedRefresh(options) {{
            const opts = Object.assign({{}}, options || {{}});
            return new Promise(function(resolve, reject) {{
              forcedRefreshQueue.push({{ opts: opts, resolve: resolve, reject: reject }});
              scheduleForcedRefreshPump();
            }});
          }}

          async function waitForInputQuietUnlessManual(opts) {{
            if (opts.reason === 'manual') return true;
            while (true) {{
              if (!hasRecentUserInput()) return true;
              await new Promise(function(resolve) {{
    setTimeout(resolve, userInputQuietDelayMs() + 100);
              }});
            }}
          }}

          async function executeForcedRefresh(opts) {{
            if (opts.preserveUserScroll && opts.scrollToBottom !== false) {{
              const term = termForHistoryAction();
              if (term && !terminalIsAtBottom(term)) {{
    opts.scrollToBottom = false;
              }}
            }}
            await waitForInputQuietUnlessManual(opts);
            const term = termForHistoryAction();
            if (!term) return false;

            return await new Promise(function(resolve) {{
              runBufferedCaptureHistorySync(term, {{
    fetchLines: FULL_HISTORY_LINES,
    onSuccess: function() {{
      resyncing = false;
      resetResyncPressure();
      resolve(true);
    }},
    onError: function(message) {{
      console.debug('claude-hub history refresh failed', message);
      resyncing = false;
      flushResyncBuffer();
      resolve(false);
    }},
    applySnapshot: function(snapshot, payload, wt, done) {{
      if (opts.preserveUserScroll && opts.scrollToBottom !== false) {{
        const termNow = termForHistoryAction();
        if (termNow && !terminalIsAtBottom(termNow)) {{
          opts.scrollToBottom = false;
        }}
      }}
      applyHistoryPayloadWithOptions(term, payload, opts, wt, function() {{
        done();
      }});
    }},
              }});
            }});
          }}

          async function refreshHistoryFromTmux(options) {{
            return enqueueForcedRefresh(options);
          }}

          function refreshAgentHistoryViewWhenBottom() {{
            if (!PROTECT_AGENT_HISTORY_VIEW || !agentHistoryViewNeedsSnapshot || !viewportIsAtBottom()) return;
            const service = bufferService();
            if (service) {{
              service.isUserScrolling = false;
            }}
            agentHistoryViewNeedsSnapshot = false;
            pendingWhenBottom = false;
            refreshHistoryFromTmux({{ reason: 'agent-return-bottom', scrollToBottom: true }});
          }}

          function noteLiveWrite(wasAtBottom, data) {{
            writeGeneration++;
            noteResyncPressure(data);
            if (wasAtBottom || isAtBottom()) {{
              pendingWhenBottom = false;
              scheduleResync(false);
            }} else {{
              pendingWhenBottom = true;
            }}
          }}

          term.write = function(data, cb) {{
            if (resyncing) {{
              resyncBuffer.push({{ data, cb }});
              return undefined;
            }}
            const wasAtBottom = isAtBottom();
            if (PROTECT_AGENT_HISTORY_VIEW && !wasAtBottom) {{
              agentHistoryViewNeedsSnapshot = true;
              pendingWhenBottom = true;
              writeGeneration++;
              if (typeof cb === 'function') {{
    setTimeout(cb, 0);
              }}
              return undefined;
            }}
            const generationAtWrite = userScrollGeneration;
            const wrappedCb = typeof cb === 'function' ? function() {{
              if (wasAtBottom) {{
    scheduleBottomFollow(generationAtWrite);
              }}
              cb();
            }} : undefined;
            const result = writeThrough(data, wrappedCb);
            if (wasAtBottom) {{
              scheduleBottomFollow(generationAtWrite);
            }}
            noteLiveWrite(wasAtBottom, data);
            return result;
          }};

          term.__claudeHubRefreshHistory = refreshHistoryFromTmux;
          term.__claudeHubScrollToBottom = function() {{
            scrollTerminalToBottom(term);
            refreshAgentHistoryViewWhenBottom();
          }};

          const viewportEl = viewportElement();
          if (viewportEl) {{
            viewportEl.addEventListener('scroll', function() {{
              recomputeDomAtBottom();
              if (pendingWhenBottom && (agentHistoryViewNeedsSnapshot ? viewportIsAtBottom() : isAtBottom())) {{
    if (agentHistoryViewNeedsSnapshot) {{
      refreshAgentHistoryViewWhenBottom();
    }} else {{
      pendingWhenBottom = false;
      scheduleResync(false);
    }}
              }}
            }}, {{ passive: true }});
          }}

          // Run one idle reconciliation after setup when authoritative replay has
          // not yet populated tmux history into xterm (lazy session creation).
          if (initialReplayNeedsReconcile) {{
            scheduleResync(true);
          }}

          async function runResync() {{
            timer = null;
            if (forcedRefreshPumpPromise || forcedRefreshQueue.length > 0) {{
              return;
            }}
            if (hasRecentUserInput()) {{
              scheduleResync(false);
              return;
            }}
            if (!isAtBottom() || resyncing) {{
              pendingWhenBottom = !isAtBottom();
              return;
            }}

            // Set resyncing BEFORE the async fetch so any concurrent
            // term.write calls land in resyncBuffer instead of slipping
            // through and forcing us to abort + retry forever under a
            // hot live-write stream (e.g. Claude TUI redraws).
            const resyncId = nextAutoResyncId++;
            const owner = beginHistorySyncOwner();
            activeAutoResyncId = resyncId;
            const inputGenerationAtStart = userInputGeneration;
            resyncing = true;
            let snapshot = null;
            try {{
              snapshot = await fetchHistorySnapshot();
            }} catch (error) {{
              console.debug('claude-hub history resync failed', error);
              cancelledAutoResyncIds.delete(resyncId);
              if (activeAutoResyncId === resyncId) {{
    activeAutoResyncId = null;
    resyncing = false;
    flushResyncBuffer();
              }}
              return;
            }}

            const isCurrentResync = activeAutoResyncId === resyncId;
            if (
              cancelledAutoResyncIds.has(resyncId) ||
              !isCurrentResync ||
              userInputGeneration !== inputGenerationAtStart ||
              hasRecentUserInput()
            ) {{
              cancelledAutoResyncIds.delete(resyncId);
              if (isCurrentResync) {{
    activeAutoResyncId = null;
    resyncing = false;
    flushResyncBuffer();
    scheduleResyncAfterInputQuiet();
              }}
              return;
            }}

            if (!isAtBottom()) {{
              // User scrolled away while we were fetching; abandon the
              // snapshot but flush any buffered live writes so nothing
              // is lost.
              cancelledAutoResyncIds.delete(resyncId);
              if (activeAutoResyncId === resyncId) activeAutoResyncId = null;
              resyncing = false;
              flushResyncBuffer();
              pendingWhenBottom = true;
              return;
            }}

            const payload = fullReplayPayloadForSnapshot(snapshot);
            if (!payload) {{
              cancelledAutoResyncIds.delete(resyncId);
              if (activeAutoResyncId === resyncId) activeAutoResyncId = null;
              resyncing = false;
              flushResyncBuffer();
              resetResyncPressure();
              return;
            }}

            writeThrough(payload, function() {{
              flushResyncBufferThen(function() {{
    if (cancelledAutoResyncIds.has(resyncId)) {{
      cancelledAutoResyncIds.delete(resyncId);
      if (activeAutoResyncId === resyncId) {{
        activeAutoResyncId = null;
        resyncing = false;
        scheduleResyncAfterInputQuiet();
      }}
      return;
    }}
    if (activeAutoResyncId !== resyncId) {{
      return;
    }}
    activeAutoResyncId = null;
    resyncing = false;
    resetResyncPressure();
    scrollTerminalToBottom(term, {{ refresh: true }});
    domAtBottomCached = true;
    if (resyncBuffer.length > 0) {{
      scheduleResync(false);
    }}
              }});
            }});
          }}
        }}

        function termForHistoryAction() {{
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

        // ---- Mobile scrolling: native inertia ----
        // xterm.js registers touchstart/touchmove on the .xterm element (parent).
        // Its handleTouchMove does scrollTop += delta manually — no inertia.
        // Its _innerRefresh snaps scrollTop = ydisp * rowHeight each frame.
        //
        // Strategy:
        // 1. Override viewport.handleTouchMove/handleTouchStart to no-ops so
        //    xterm's listener callback does nothing useful even if it fires
        // 2. Let browser-native scroll on .xterm-viewport (overflow-y:scroll) work
        // 3. Keep xterm's _innerRefresh intact. Live output can advance
        //    viewportY/baseY while the user is touching history; blocking
        //    _innerRefresh lets scrollTop go stale and makes rendered
        //    history appear mixed with new output until touchend.
        var _isTouchScrolling = false;
        var _touchScrollTimer = null;

        function _getTerm() {{
          var term = window.ttyd && window.ttyd.terminal ? window.ttyd.terminal : window.term;
          return term || null;
        }}

        function _getViewportObj() {{
          var term = _getTerm();
          return term ? (term.viewport || (term._core && term._core.viewport) || null) : null;
        }}

        // ---- Mobile "select text" mode ----
        // xterm.js v4 has no native touch text-selection: touch only scrolls.
        // On touch devices we keep passthrough scrolling by default (see the
        // pointer:coarse CSS above) and let the user opt into selection via an
        // explicit toggle (MobileControls "选择" button -> terminal-select-mode
        // message). While active, single-finger touches are translated into the
        // synthetic mouse drag that xterm's own SelectionService understands,
        // so selection semantics (partial first/last row, multi-row) match the
        // desktop mouse exactly. On lift, the selection is copied to the
        // clipboard and the parent is notified for a toast.
        var _selectModeActive = false;
        var _selTouchActive = false;

        var _selAnchor = null;

        // ---- Long-press to select (no mode toggle required) ----
        // The explicit "选择" toggle above still works, but the natural mobile
        // gesture is a long-press: hold a finger still on the text for a moment
        // to start a selection, drag to extend, lift to copy. We arm a timer on
        // touchstart and only activate selection if the finger stays roughly
        // still until it fires; any early movement cancels the timer so a normal
        // swipe still scrolls with native inertia.
        var LONG_PRESS_MS = 450;
        var LONG_PRESS_MOVE_TOLERANCE_PX = 10;
        var _longPressTimer = null;
        var _longPressSelectionShown = false;
        var _touchDown = false;
        var _touchStartX = 0;
        var _touchStartY = 0;

        function _screenEl() {{
          return document.querySelector('.xterm-screen');
        }}

        // Map a viewport (clientX, clientY) point to an absolute buffer cell
        // [col, absRow]. xterm.js v4 ignores synthetic MouseEvents for
        // selection (it trusts real input only), so touch selection drives the
        // selection model directly from computed cells instead.
        function _cellFromPoint(clientX, clientY) {{
          var term = _getTerm();
          var screen = _screenEl();
          if (!term || !screen) return null;
          var rect = screen.getBoundingClientRect();
          var core = term._core;
          var dims = core && core._renderService && core._renderService.dimensions;
          var cw = dims && dims.css && dims.css.cell ? dims.css.cell.width : (dims ? dims.actualCellWidth : 0);
          var ch = dims && dims.css && dims.css.cell ? dims.css.cell.height : (dims ? dims.actualCellHeight : 0);
          if (!cw || !ch) return null;
          var cols = term.cols || 80;
          var rows = term.rows || 24;
          var col = Math.floor((clientX - rect.left) / cw);
          var srow = Math.floor((clientY - rect.top) / ch);
          col = Math.max(0, Math.min(cols - 1, col));
          srow = Math.max(0, Math.min(rows - 1, srow));
          var vy = term.buffer && term.buffer.active ? term.buffer.active.viewportY : 0;
          return [col, vy + srow];
        }}

        // Apply a selection from anchor cell to focus cell via the selection
        // model, ordering endpoints so a backward (upward) drag still works.
        function _applySelection(anchor, focus) {{
          var term = _getTerm();
          var ss = term && term._core && term._core._selectionService;
          if (!ss || !ss._model || !anchor || !focus) return;
          var start = anchor;
          var end = focus;
          if (end[1] < start[1] || (end[1] === start[1] && end[0] < start[0])) {{
            start = focus;
            end = anchor;
          }}
          ss._model.selectionStart = [start[0], start[1]];
          // selectionEnd is exclusive on the column, so +1 includes the focused
          // cell under the finger.
          ss._model.selectionEnd = [end[0] + 1, end[1]];
          if (typeof ss.refresh === 'function') ss.refresh();
        }}

        function _fallbackCopy(text) {{
          try {{
            var ta = document.createElement('textarea');
            ta.value = text;
            ta.setAttribute('readonly', '');
            ta.style.position = 'fixed';
            ta.style.top = '-1000px';
            ta.style.opacity = '0';
            document.body.appendChild(ta);
            ta.focus();
            ta.select();
            var ok = document.execCommand('copy');
            document.body.removeChild(ta);
            return ok;
          }} catch (error) {{
            return false;
          }}
        }}

        function _notifyParentSelect(status, length) {{
          try {{
            var target = window.parent && window.parent !== window ? window.parent : window;
            target.postMessage({{
              type: 'terminal-select-copied',
              tabId: TAB_ID,
              status: status,
              length: length || 0,
            }}, '*');
          }} catch (error) {{}}
        }}

        function copyCurrentSelection() {{
          var term = _getTerm();
          var text = term && typeof term.getSelection === 'function' ? term.getSelection() : '';
          if (!text) {{
            _notifyParentSelect('empty', 0);
            return;
          }}
          var finish = function(ok) {{
            _notifyParentSelect(ok ? 'copied' : 'failed', text.length);
          }};
          try {{
            if (navigator.clipboard && navigator.clipboard.writeText) {{
              navigator.clipboard.writeText(text).then(function() {{
    finish(true);
              }}, function() {{
    finish(_fallbackCopy(text));
              }});
              return;
            }}
          }} catch (error) {{}}
          finish(_fallbackCopy(text));
        }}

        function setSelectMode(on) {{
          _selectModeActive = !!on;
          try {{
            if (_selectModeActive) {{
              document.documentElement.setAttribute('data-select-mode', 'on');
            }} else {{
              document.documentElement.removeAttribute('data-select-mode');
              _selTouchActive = false;
              var term = _getTerm();
              if (term && typeof term.clearSelection === 'function') {{
    term.clearSelection();
              }}
            }}
          }} catch (error) {{}}
        }}

        function _cancelLongPress() {{
          if (_longPressTimer !== null) {{
            clearTimeout(_longPressTimer);
            _longPressTimer = null;
          }}
        }}

        // Select the whole word under a cell for a useful long-press-then-lift
        // (copying a single character is rarely what the user wants). Returns
        // true when a non-empty word selection was produced; the caller falls
        // back to a single-cell anchor otherwise. Dragging afterwards extends
        // the selection cell-by-cell from the anchor, matching select-mode drag.
        function _selectWordAtCell(cell) {{
          try {{
            var term = _getTerm();
            var ss = term && term._core && term._core._selectionService;
            // Prefer xterm's own word selection when this build exposes it
            // (handles wide glyphs correctly).
            if (ss && typeof ss.selectWordAt === 'function') {{
              ss.selectWordAt([cell[0], cell[1]], false);
              if (typeof ss.refresh === 'function') ss.refresh();
              if (term && typeof term.getSelection === 'function' && term.getSelection()) {{
    return true;
              }}
            }}
            // Manual fallback: derive the word span from the buffer line text.
            var buf = term && term.buffer && term.buffer.active;
            var line = buf && typeof buf.getLine === 'function' ? buf.getLine(cell[1]) : null;
            if (!line || typeof line.translateToString !== 'function') return false;
            var text = line.translateToString(true);
            var col = cell[0];
            if (col < 0 || col >= text.length) return false;
            // Cell index only maps 1:1 to the string without wide/non-latin
            // glyphs; skip word selection on such lines so we never highlight
            // the wrong span (cell-precise drag selection still works there).
            if (/[^\\x00-\\xff]/.test(text)) return false;
            var isWord = function(c) {{ return !!c && !/\\s/.test(c); }};
            if (!isWord(text.charAt(col))) return false;
            var start = col;
            var end = col;
            while (start > 0 && isWord(text.charAt(start - 1))) start--;
            while (end < text.length - 1 && isWord(text.charAt(end + 1))) end++;
            _selAnchor = [start, cell[1]];
            _applySelection([start, cell[1]], [end, cell[1]]);
            return true;
          }} catch (error) {{}}
          return false;
        }}

        function _activateLongPressSelection() {{
          _longPressTimer = null;
          if (!_touchDown || _selectModeActive) return;
          var cell = _cellFromPoint(_touchStartX, _touchStartY);
          if (!cell) return;
          _longPressSelectionShown = true;
          _selTouchActive = true;
          _selAnchor = cell;
          // Select the whole word under the finger so a long-press-then-lift
          // copies something useful; drag afterwards extends from there.
          if (!_selectWordAtCell(cell)) {{
            _applySelection(cell, cell);
          }}
          // Light haptic confirmation where supported (Android). Best-effort.
          try {{ if (navigator.vibrate) navigator.vibrate(15); }} catch (error) {{}}
        }}

        function selectTouchStart(event) {{
          if (!event.touches || event.touches.length !== 1) return;
          var t = event.touches[0];

          // Explicit select mode: keep the original tap-to-anchor behavior.
          if (_selectModeActive) {{
            var cell = _cellFromPoint(t.clientX, t.clientY);
            if (!cell) return;
            _selTouchActive = true;
            _selAnchor = cell;
            _applySelection(cell, cell);
            event.preventDefault();
            event.stopPropagation();
            return;
          }}

          // Implicit long-press path. Do NOT preventDefault here so a swipe can
          // still start a native scroll; the timer only fires on a still hold.
          if (_longPressSelectionShown) {{
            // Clear a lingering long-press highlight when a new touch begins.
            _longPressSelectionShown = false;
            var prevTerm = _getTerm();
            if (prevTerm && typeof prevTerm.clearSelection === 'function') {{
              try {{ prevTerm.clearSelection(); }} catch (error) {{}}
            }}
          }}
          _touchDown = true;
          _touchStartX = t.clientX;
          _touchStartY = t.clientY;
          _cancelLongPress();
          _longPressTimer = setTimeout(_activateLongPressSelection, LONG_PRESS_MS);
        }}

        function selectTouchMove(event) {{
          if (!event.touches || event.touches.length !== 1) return;
          var t = event.touches[0];

          // Arming phase: a moved finger means "scroll", not "select".
          if (_longPressTimer !== null && !_selTouchActive) {{
            var dx = Math.abs(t.clientX - _touchStartX);
            var dy = Math.abs(t.clientY - _touchStartY);
            if (dx > LONG_PRESS_MOVE_TOLERANCE_PX || dy > LONG_PRESS_MOVE_TOLERANCE_PX) {{
              _cancelLongPress();
            }}
            return; // let native scroll proceed
          }}

          if (!_selTouchActive) return;
          var moveCell = _cellFromPoint(t.clientX, t.clientY);
          if (!moveCell || !_selAnchor) return;
          _applySelection(_selAnchor, moveCell);
          event.preventDefault();
          event.stopPropagation();
        }}

        function selectTouchEnd(event) {{
          _touchDown = false;
          _cancelLongPress();
          if (!_selTouchActive) return;
          _selTouchActive = false;
          copyCurrentSelection();
          event.preventDefault();
          event.stopPropagation();
        }}

        function _hookSelectTouch(xtermEl) {{
          if (!xtermEl || xtermEl.__claudeHubSelectHooked) return;
          xtermEl.__claudeHubSelectHooked = true;
          // Capture phase + non-passive so we run before (and can suppress) the
          // native-inertia scroll listeners on the viewport while selecting.
          xtermEl.addEventListener('touchstart', selectTouchStart, {{ capture: true, passive: false }});
          xtermEl.addEventListener('touchmove', selectTouchMove, {{ capture: true, passive: false }});
          xtermEl.addEventListener('touchend', selectTouchEnd, {{ capture: true, passive: false }});
          xtermEl.addEventListener('touchcancel', selectTouchEnd, {{ capture: true, passive: false }});
        }}

        function _markUserScrolling() {{
          var term = _getTerm();
          var bufferService = term && term._core && term._core._bufferService;
          if (bufferService) {{
            bufferService.isUserScrolling = true;
          }}
        }}

        function _scrollAwayFromBottom(deltaY) {{
          var term = _getTerm();
          var buffer = term && term.buffer && term.buffer.active;
          if (!term || !buffer || buffer.viewportY < buffer.baseY - 1) return;

          var vpObj = _getViewportObj();
          var rowHeight = (vpObj && vpObj._currentRowHeight) || 15;
          var lines = Math.max(1, Math.ceil(Math.abs(deltaY) / rowHeight));
          if (typeof term.scrollLines === 'function') {{
            term.scrollLines(-lines);
            return;
          }}

          var viewportEl = document.querySelector('.xterm-viewport');
          if (viewportEl) {{
            viewportEl.scrollTop = Math.max(0, viewportEl.scrollTop - lines * rowHeight);
          }}
        }}

        function enableNativeTouchScroll() {{
          var vpObj = _getViewportObj();
          var viewportEl = document.querySelector('.xterm-viewport');
          var xtermEl = document.querySelector('.xterm');
          if (!vpObj || !viewportEl) return false;
          if (vpObj.__claudeHubNativeTouch) return true;
          vpObj.__claudeHubNativeTouch = true;

          // 1. Neutralize xterm's touch handlers on the viewport object.
          //    xterm's touchmove listener on .xterm calls viewport.handleTouchMove()
          //    which does scrollTop += delta. Replace with no-op.
          vpObj.handleTouchStart = function() {{}};
          vpObj.handleTouchMove = function() {{ return true; }};

          // Wire the explicit "select text" touch handlers onto the .xterm root
          // so they run in the capture phase before native scrolling.
          _hookSelectTouch(xtermEl);

          // Desktop wheel/trackpad scrolls can race with fast live output
          // while the viewport is still at the bottom. Mark user scrolling
          // in the capture phase so xterm stops following new output before
          // its normal wheel handler processes the scroll delta.
          function handleWheelStart(event) {{
            const target = event.target;
            const insideTerminal = !target || target === window || target === document ||
              (target.closest && target.closest('.xterm'));
            if (insideTerminal && event.deltaY < 0) {{
              noteUserScrollIntent();
              _markUserScrolling();
              _scrollAwayFromBottom(event.deltaY);
            }}
          }}
          viewportEl.addEventListener('wheel', handleWheelStart, {{ capture: true, passive: true }});
          if (xtermEl) {{
            xtermEl.addEventListener('wheel', handleWheelStart, {{ capture: true, passive: true }});
          }}
          window.addEventListener('wheel', handleWheelStart, {{ capture: true, passive: true }});

          // 2. Track touch state on the viewport element
          var touchStartY = null;
          viewportEl.addEventListener('touchstart', function(event) {{
            _isTouchScrolling = true;
            touchStartY = null;
            if (event.touches && event.touches.length > 0) {{
              touchStartY = event.touches[0].clientY;
            }}
            if (_touchScrollTimer) {{
              clearTimeout(_touchScrollTimer);
              _touchScrollTimer = null;
            }}
          }}, {{ passive: true }});

          viewportEl.addEventListener('touchmove', function(event) {{
            if (touchStartY !== null && event.touches && event.touches.length > 0) {{
              if (event.touches[0].clientY - touchStartY > 8) {{
    noteUserScrollIntent();
    _markUserScrolling();
              }}
            }}
          }}, {{ passive: true }});

          viewportEl.addEventListener('touchend', function() {{
            _touchScrollTimer = setTimeout(function() {{
              _isTouchScrolling = false;
              _touchScrollTimer = null;
            }}, 500);
          }}, {{ passive: true }});

          return true;
        }}

        // Poll until both viewport object and DOM element are ready
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
