"""Automatic terminal history resynchronization fragment."""


def render_history_resync() -> str:
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

"""
