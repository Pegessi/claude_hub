"""Bootstrap half of the injected terminal history script."""

import json


def render_terminal_bootstrap(
    *,
    tab_id: str,
    agent_type_value: str,
    execution_target_value: str,
    full_history_lines: int,
    initial_agent_replay_min_lines: int,
    initial_agent_history_lines: int,
    initial_terminal_history_lines: int,
) -> str:
    return f"""
    <script>
      (function () {{
        const TAB_ID = {json.dumps(tab_id)};
        const AGENT_TYPE = {json.dumps(agent_type_value)};
        const EXECUTION_TARGET = {json.dumps(execution_target_value)};
        const IS_AGENT_TUI = AGENT_TYPE === 'claude' || AGENT_TYPE === 'codex' || AGENT_TYPE === 'traex' || AGENT_TYPE === 'cursor';
        const IS_REMOTE_AGENT_TUI = EXECUTION_TARGET === 'remote' && IS_AGENT_TUI;
        const FULL_HISTORY_LINES = {full_history_lines};
        const INITIAL_AGENT_REPLAY_MIN_LINES = {initial_agent_replay_min_lines};
        const INITIAL_HISTORY_LINES = IS_AGENT_TUI
          ? {initial_agent_history_lines}
          : {initial_terminal_history_lines};
        let historyText = '';
        let historyCursorX = null;
        let historyCursorY = null;
        let historyLoaded = false;
        let bootstrapCorrelationRequestId = null;
        let bootstrapCorrelationAgentStatus = null;
        const bootstrapReadyPostedRequestIds = new Set();
        const bootstrapPendingRequestIds = new Set();
        let replayed = false;
        let historyPreloadState = (IS_REMOTE_AGENT_TUI || AGENT_TYPE === 'terminal') ? 'skipped' : 'pending';
        let historyPreloadError = null;
        let initialReplayNeedsReconcile = true;
        const HISTORY_FETCH_TIMEOUT_MS = 15000;
        const BUFFERED_CAPTURE_QUIET_MS = 150;
        const BUFFERED_CAPTURE_MAX_FETCH_ATTEMPTS = 12;

        function postReadyAfterPaint(callback) {{
          if (typeof requestAnimationFrame === 'function') {{
            requestAnimationFrame(function() {{ requestAnimationFrame(callback); }});
          }} else {{
            callback();
          }}
        }}

        function failBootstrapCorrelations(message, primaryRequestId) {{
          const ids = new Set(bootstrapPendingRequestIds);
          bootstrapPendingRequestIds.clear();
          if (primaryRequestId) ids.add(primaryRequestId);
          ids.forEach(function(rid) {{
            if (!rid || bootstrapReadyPostedRequestIds.has(rid)) return;
            bootstrapReadyPostedRequestIds.add(rid);
            postHistoryRefreshResult('bootstrap', false, message, rid);
          }});
        }}

        function postBootstrapDocumentReady(requestId) {{
          const rid = requestId || bootstrapCorrelationRequestId;
          if (!rid || bootstrapReadyPostedRequestIds.has(rid)) return;
          const term = termForHistoryAction();
          if (term && term.__claudeHubInitialReplayFailed) {{
            runBootstrapRecoveryRefresh(rid);
            return;
          }}
          if (historyPreloadState === 'failed') {{
            runBootstrapRecoveryRefresh(rid);
            return;
          }}
          if (initialReplayNeedsReconcile) {{
            runBootstrapRecoveryRefresh(rid);
            return;
          }}
          if (!term || !term.__claudeHubReplayDone) return;
          if (term.__claudeHubReplayBuffering === true) {{
            bootstrapPendingRequestIds.add(rid);
            return;
          }}
          bootstrapReadyPostedRequestIds.add(rid);
          postReadyAfterPaint(function() {{
            postHistoryRefreshResult('bootstrap', true, null, rid);
          }});
        }}

        function tryPostBootstrapForCorrelation(requestId) {{
          if (!requestId) return;
          resolveBootstrapCorrelation(requestId, bootstrapCorrelationAgentStatus);
        }}

        function runBootstrapRecoveryRefresh(requestId) {{
          if (!requestId || bootstrapReadyPostedRequestIds.has(requestId)) return;
          const term = termForHistoryAction();
          if (!term || typeof term.__claudeHubRefreshHistory !== 'function') {{
            bootstrapPendingRequestIds.add(requestId);
            return;
          }}
          Promise.resolve(term.__claudeHubRefreshHistory({{
            reason: 'bootstrap',
            scrollToBottom: true,
            requestId: requestId,
          }})).then(function(ok) {{
            if (bootstrapReadyPostedRequestIds.has(requestId)) return;
            bootstrapReadyPostedRequestIds.add(requestId);
            if (ok !== false) {{
              initialReplayNeedsReconcile = false;
              if (term.__claudeHubInitialReplayFailed) {{
    term.__claudeHubInitialReplayFailed = false;
              }}
              if (historyPreloadState === 'failed') {{
    historyPreloadState = 'ok';
    historyPreloadError = null;
              }}
              postReadyAfterPaint(function() {{
    postHistoryRefreshResult('bootstrap', true, null, requestId);
              }});
              return;
            }}
            postHistoryRefreshResult(
              'bootstrap',
              false,
              'bootstrap recovery refresh failed',
              requestId,
            );
          }}).catch(function(error) {{
            if (bootstrapReadyPostedRequestIds.has(requestId)) return;
            bootstrapReadyPostedRequestIds.add(requestId);
            postHistoryRefreshResult(
              'bootstrap',
              false,
              error && error.message ? error.message : 'bootstrap recovery refresh failed',
              requestId,
            );
          }});
        }}

        function retryLocalAgentPreloadThenRefresh(requestId) {{
          historyPreloadState = 'pending';
          fetchHistorySnapshot(INITIAL_HISTORY_LINES, {{ timeoutMs: HISTORY_FETCH_TIMEOUT_MS }})
            .then(function(snapshot) {{
              historyText = snapshot.history;
              historyCursorX = snapshot.cursorX;
              historyCursorY = snapshot.cursorY;
              historyPreloadState = 'ok';
              historyPreloadError = null;
            }})
            .catch(function(error) {{
              historyPreloadState = 'failed';
              historyPreloadError = error && error.message ? error.message : 'history preload failed';
            }})
            .finally(function() {{
              runBootstrapRecoveryRefresh(requestId);
            }});
        }}

        function flushPendingBootstrapCorrelations() {{
          const pending = Array.from(bootstrapPendingRequestIds);
          bootstrapPendingRequestIds.clear();
          pending.forEach(function(rid) {{ postBootstrapDocumentReady(rid); }});
          postBootstrapDocumentReady(bootstrapCorrelationRequestId);
        }}

        function agentStatusIsStable(agentStatus) {{
          return agentStatus === 'idle' || agentStatus === 'attention';
        }}

        function finishRemoteStableBootstrapReady(requestId) {{
          if (!requestId || bootstrapReadyPostedRequestIds.has(requestId)) return;
          bootstrapReadyPostedRequestIds.add(requestId);
          postReadyAfterPaint(function() {{
            postHistoryRefreshResult('bootstrap', true, null, requestId);
          }});
        }}

        function completeRemoteLiveBootstrap(requestId) {{
          function attempt(attemptsLeft) {{
            const term = termForHistoryAction();
            if (term && term.__claudeHubReplayDone && term.__claudeHubReplayBuffering !== true) {{
              if (bootstrapReadyPostedRequestIds.has(requestId)) return;
              bootstrapReadyPostedRequestIds.add(requestId);
              postReadyAfterPaint(function() {{
    postHistoryRefreshResult('bootstrap', true, null, requestId, {{ deferredRecovery: true }});
              }});
              return;
            }}
            if (attemptsLeft > 0) {{
              setTimeout(function() {{ attempt(attemptsLeft - 1); }}, 50);
            }} else {{
              failBootstrapCorrelations('terminal not ready', requestId);
            }}
          }}
          attempt(80);
        }}

        function completeRemoteStableBootstrap(requestId) {{
          function attempt(attemptsLeft) {{
            const term = termForHistoryAction();
            if (!term || typeof term.write !== 'function') {{
              if (attemptsLeft > 0) {{
    setTimeout(function() {{ attempt(attemptsLeft - 1); }}, 50);
              }} else {{
    failBootstrapCorrelations('terminal not ready', requestId);
              }}
              return;
            }}
            if (term.__claudeHubRemoteStableBootstrapInFlight) {{
              bootstrapPendingRequestIds.add(requestId);
              return;
            }}
            runRemoteStableBootstrap(term, requestId);
          }}
          attempt(80);
        }}

        function runBufferedCaptureHistorySync(term, options) {{
          const fetchLines = options && options.fetchLines;
          const maxFetchAttempts = (options && options.maxFetchAttempts) || BUFFERED_CAPTURE_MAX_FETCH_ATTEMPTS;
          const captureQuietMs = (options && options.captureQuietMs) || BUFFERED_CAPTURE_QUIET_MS;
          const fetchTimeoutMs = (options && options.fetchTimeoutMs) || HISTORY_FETCH_TIMEOUT_MS;
          const originalWrite = term.write;
          const innerWriteFn = (
            typeof term.__claudeHubInnerWrite === 'function'
          ) ? term.__claudeHubInnerWrite : null;
          const captureBuffer = [];
          const refetchFallback = [];
          let lastCaptureBufferedAt = 0;
          let fetchAttempts = 0;
          let phase = 'capturing';
          let inputListenerRegistered = false;

          function isCapturePhase() {{
            return phase === 'capturing' || phase === 'finalizing';
          }}

          function invokeWrite(data, cb) {{
            let cbCalled = false;
            let writerReturned = false;
            function wrappedCb() {{
              if (cbCalled) return;
              cbCalled = true;
              if (writerReturned && typeof cb === 'function') {{
    cb();
              }}
            }}
            try {{
              if (innerWriteFn) {{
    innerWriteFn.call(term, data, wrappedCb);
              }} else {{
    originalWrite.call(term, data, wrappedCb);
              }}
              writerReturned = true;
              if (cbCalled && typeof cb === 'function') {{
    cb();
              }}
            }} catch (error) {{
              writerReturned = true;
              throw error;
            }}
          }}

          function captureWrite(data, cb) {{
            if (isCapturePhase()) {{
              captureBuffer.push({{ data: data, cb: cb, cbInvoked: false }});
              lastCaptureBufferedAt = Date.now();
              return undefined;
            }}
            return originalWrite.call(term, data, cb);
          }}
          term.write = captureWrite;

          function restoreWriteOnce() {{
            if (term.write === captureWrite) {{
              term.write = originalWrite;
            }}
          }}

          function removeInputListener() {{
            if (!inputListenerRegistered) return;
            inputListenerRegistered = false;
            const index = terminalUserInputListeners.indexOf(releaseForUserInput);
            if (index >= 0) terminalUserInputListeners.splice(index, 1);
          }}

          function emitResultOnce(success, detail) {{
            if (phase === 'settled') return;
            phase = 'settled';
            removeInputListener();
            restoreWriteOnce();
            try {{
              if (success) {{
    if (options && typeof options.onSuccess === 'function') options.onSuccess();
              }} else if (options && typeof options.onError === 'function') {{
    options.onError(detail || 'failed');
              }}
            }} catch (callbackError) {{
              console.debug('claude-hub capture coordinator callback failed', callbackError);
            }}
          }}

          function invokeItemCbOnce(item) {{
            if (!item || item.cbInvoked) return;
            item.cbInvoked = true;
            try {{
              if (typeof item.cb === 'function') item.cb();
            }} catch (cbError) {{
              console.debug('claude-hub capture drain callback failed', cbError);
            }}
          }}

          function discardRemainingCaptureCallbacks() {{
            const remaining = captureBuffer.splice(0, captureBuffer.length);
            remaining.forEach(invokeItemCbOnce);
          }}

          function releaseForUserInput() {{
            if (phase === 'settled') return;
            // The fetched snapshot remains the visible baseline. Buffered WS
            // frames all predate this keypress and may be recovered from tmux,
            // so restore live writes before acknowledging them. A write callback
            // may synchronously release another WS frame; after the phase change
            // that frame must go directly to xterm rather than back into capture.
            phase = 'settled';
            removeInputListener();
            restoreWriteOnce();
            discardRemainingCaptureCallbacks();
            clearRefetchFallback();
            if (options && typeof options.onUserInput === 'function') {{
              options.onUserInput();
            }}
          }}

          function deferCaptureToRefetchFallback() {{
            const pending = captureBuffer.splice(0, captureBuffer.length);
            pending.forEach(function(item) {{
              invokeItemCbOnce(item);
              refetchFallback.push({{
    data: item.data,
    cb: null,
    cbInvoked: true,
              }});
            }});
          }}

          function restoreRefetchFallbackToCaptureBuffer() {{
            if (refetchFallback.length === 0) return;
            captureBuffer.unshift.apply(captureBuffer, refetchFallback.splice(0, refetchFallback.length));
          }}

          function clearRefetchFallback() {{
            refetchFallback.length = 0;
          }}

          function abortFinalize(message) {{
            if (phase === 'settled') return;
            discardRemainingCaptureCallbacks();
            emitResultOnce(false, message);
          }}

          function drainCaptureBufferThen(callback) {{
            function drainNext() {{
              if (phase === 'settled') return;
              if (captureBuffer.length === 0) {{
    if (typeof callback === 'function') callback();
    return;
              }}
              const item = captureBuffer.shift();
              try {{
    invokeWrite(item.data, function() {{
      invokeItemCbOnce(item);
      if (phase === 'settled') return;
      drainNext();
    }});
              }} catch (error) {{
    invokeItemCbOnce(item);
    abortFinalize(error && error.message ? error.message : 'drain write failed');
              }}
            }}
            drainNext();
          }}

          function completeFinalize(success, detail) {{
            if (phase === 'settled' || phase === 'finalizing') return;
            phase = 'finalizing';
            try {{
              drainCaptureBufferThen(function() {{
    if (phase === 'settled') return;
    emitResultOnce(success, detail);
              }});
            }} catch (error) {{
              abortFinalize(error && error.message ? error.message : 'finalize failed');
            }}
          }}

          function finishSuccess() {{
            if (phase === 'settled' || phase === 'finalizing') return;
            completeFinalize(true);
          }}

          function finishFailure(message) {{
            if (phase === 'settled' || phase === 'finalizing') return;
            restoreRefetchFallbackToCaptureBuffer();
            completeFinalize(false, message);
          }}

          function waitForCaptureQuietThen(callback) {{
            function check() {{
              if (phase === 'settled') return;
              if (captureBuffer.length > 0) {{
    deferCaptureToRefetchFallback();
              }}
              const quietFor = lastCaptureBufferedAt > 0
    ? Date.now() - lastCaptureBufferedAt
    : captureQuietMs;
              if (quietFor < captureQuietMs) {{
    setTimeout(check, captureQuietMs - quietFor + 10);
    return;
              }}
              callback();
            }}
            check();
          }}

          function applyResolvedSnapshot(snapshot) {{
            if (captureBuffer.length > 0) {{
              deferCaptureToRefetchFallback();
              waitForCaptureQuietThen(attemptFetch);
              return;
            }}

            const payload = fullReplayPayloadForSnapshot(snapshot);
            if (!payload) {{
              clearRefetchFallback();
              finishSuccess();
              return;
            }}

            // Snapshot boundary: pre-apply captures were already deferred
            // above; only frames arriving after this apply may finalize-drain.
            discardRemainingCaptureCallbacks();

            function onSnapshotApplied() {{
              clearRefetchFallback();
              finishSuccess();
            }}

            try {{
              if (options && typeof options.applySnapshot === 'function') {{
    options.applySnapshot(snapshot, payload, invokeWrite, onSnapshotApplied);
              }} else {{
    invokeWrite(payload, onSnapshotApplied);
              }}
            }} catch (error) {{
              finishFailure(error && error.message ? error.message : 'apply failed');
            }}
          }}

          function attemptFetch() {{
            if (phase === 'settled') return;
            fetchAttempts += 1;
            if (fetchAttempts > maxFetchAttempts) {{
              finishFailure('history sync fetch retry limit exceeded');
              return;
            }}

            fetchHistorySnapshot(fetchLines, {{ timeoutMs: fetchTimeoutMs }})
              .then(function(snapshot) {{
    if (phase === 'settled') return;
    applyResolvedSnapshot(snapshot);
              }})
              .catch(function(error) {{
    if (phase === 'settled') return;
    finishFailure(error && error.message ? error.message : 'fetch failed');
              }});
          }}

          if (options && options.releaseOnUserInput) {{
            inputListenerRegistered = true;
            terminalUserInputListeners.push(releaseForUserInput);
          }}
          attemptFetch();
        }}

        function runRemoteStableBootstrap(term, requestId) {{
          term.__claudeHubRemoteStableBootstrapInFlight = true;
          term.__claudeHubReplayBuffering = true;

          runBufferedCaptureHistorySync(term, {{
            fetchLines: INITIAL_HISTORY_LINES,
            onSuccess: function() {{
              term.__claudeHubRemoteStableBootstrapInFlight = false;
              term.__claudeHubReplayDone = true;
              term.__claudeHubReplayBuffering = false;
              initialReplayNeedsReconcile = false;
              setupHistoryResync(term);
              finishRemoteStableBootstrapReady(requestId);
              flushPendingBootstrapCorrelations();
            }},
            onError: function(message) {{
              term.__claudeHubRemoteStableBootstrapInFlight = false;
              term.__claudeHubReplayBuffering = false;
              term.__claudeHubReplayDone = true;
              setupHistoryResync(term);
              if (!bootstrapReadyPostedRequestIds.has(requestId)) {{
    bootstrapReadyPostedRequestIds.add(requestId);
    postHistoryRefreshResult('bootstrap', false, message, requestId);
              }}
              const pending = Array.from(bootstrapPendingRequestIds);
              bootstrapPendingRequestIds.clear();
              pending.forEach(function(rid) {{
    if (!bootstrapReadyPostedRequestIds.has(rid)) {{
      bootstrapReadyPostedRequestIds.add(rid);
      postHistoryRefreshResult('bootstrap', false, message, rid);
    }}
              }});
            }},
            applySnapshot: function(snapshot, payload, wt, done) {{
              wt(payload, done);
            }},
          }});
        }}

        function storeBootstrapCorrelation(requestId, agentStatus) {{
          if (agentStatus !== undefined && agentStatus !== null) {{
            bootstrapCorrelationAgentStatus = agentStatus;
          }}
          resolveBootstrapCorrelation(requestId || null, agentStatus);
        }}

        function resolveBootstrapCorrelation(requestId, agentStatus) {{
          if (!requestId) return;
          bootstrapCorrelationRequestId = requestId;
          if (IS_REMOTE_AGENT_TUI) {{
            const status = (
              agentStatus !== undefined && agentStatus !== null
            ) ? agentStatus : bootstrapCorrelationAgentStatus;
            if (agentStatusIsStable(status)) {{
              completeRemoteStableBootstrap(requestId);
            }} else {{
              completeRemoteLiveBootstrap(requestId);
            }}
            return;
          }}
          const term = termForHistoryAction();
          if (historyPreloadState === 'failed') {{
            retryLocalAgentPreloadThenRefresh(requestId);
            return;
          }}
          if (term && term.__claudeHubInitialReplayFailed) {{
            runBootstrapRecoveryRefresh(requestId);
            return;
          }}
          if (!term || !term.__claudeHubReplayDone || term.__claudeHubReplayBuffering === true) {{
            bootstrapPendingRequestIds.add(requestId);
            return;
          }}
          postBootstrapDocumentReady(requestId);
        }}

        function markHistoryLoaded() {{
          historyLoaded = true;
          tryHookTerm();
        }}

        if (IS_REMOTE_AGENT_TUI || AGENT_TYPE === 'terminal') {{
          // Remote agent TUIs and plain terminals skip preload; plain terminals
          // always fresh-fetch via the capture coordinator during replay.
          markHistoryLoaded();
        }} else {{
          fetchHistorySnapshot(INITIAL_HISTORY_LINES, {{ timeoutMs: HISTORY_FETCH_TIMEOUT_MS }})
            .then(function(snapshot) {{
              if (replayed) return;
              historyText = snapshot.history;
              historyCursorX = snapshot.cursorX;
              historyCursorY = snapshot.cursorY;
              historyPreloadState = 'ok';
            }})
            .catch(function(error) {{
              if (replayed) return;
              historyPreloadState = 'failed';
              historyPreloadError = error && error.message ? error.message : 'history preload failed';
              console.debug('claude-hub history preload failed', error);
            }})
            .finally(markHistoryLoaded);
        }}

        // NOTE: Do NOT early-return when historyText is empty.  The
        // hook and resize-guard logic below must run regardless of
        // whether there is history to replay.

        let currentTerm = undefined;
        let userScrollGeneration = 0;
        let userInputGeneration = 0;
        let lastUserInputAt = 0;
        const USER_INPUT_QUIET_MS = 1500;
        const terminalUserInputListeners = [];

        function noteUserScrollIntent() {{
          userScrollGeneration++;
        }}

        function noteTerminalUserInput() {{
          userInputGeneration++;
          lastUserInputAt = Date.now();
          terminalUserInputListeners.forEach(function(listener) {{
            try {{
              listener();
            }} catch (error) {{
              console.debug('claude-hub terminal input listener failed', error);
            }}
          }});
        }}

        function hasRecentUserInput() {{
          return lastUserInputAt > 0 && Date.now() - lastUserInputAt < USER_INPUT_QUIET_MS;
        }}

        function userInputQuietDelayMs() {{
          if (lastUserInputAt <= 0) return 0;
          return Math.max(0, USER_INPUT_QUIET_MS - (Date.now() - lastUserInputAt));
        }}

        function normalizedHistoryText() {{
          return historyText.replace(/\\r?\\n/g, '\\r\\n');
        }}

        function cursorSeq(cursorX, cursorY) {{
          if (!Number.isInteger(cursorX) || !Number.isInteger(cursorY)) return '';
          return '\\x1b[' + (cursorY + 1) + ';' + (cursorX + 1) + 'H';
        }}

        function snapshotFromPayload(payload) {{
          return {{
            history: payload && typeof payload.history === 'string' ? payload.history : '',
            cursorX: payload && Number.isInteger(payload.cursor_x) ? payload.cursor_x : null,
            cursorY: payload && Number.isInteger(payload.cursor_y) ? payload.cursor_y : null,
          }};
        }}

        async function fetchHistorySnapshot(lines, options) {{
          const requestedLines = Number.isInteger(lines) && lines > 0 ? lines : FULL_HISTORY_LINES;
          const timeoutMs = (options && options.timeoutMs) || HISTORY_FETCH_TIMEOUT_MS;
          const controller = new AbortController();
          const timeoutId = setTimeout(function() {{
            controller.abort();
          }}, timeoutMs);
          try {{
            const response = await fetch(`/api/terminal/history/${{TAB_ID}}?lines=${{requestedLines}}`, {{
              cache: 'no-store',
              credentials: 'same-origin',
              signal: controller.signal,
            }});
            if (!response.ok) throw new Error('history fetch failed: ' + response.status);
            return snapshotFromPayload(await response.json());
          }} catch (error) {{
            if (error && error.name === 'AbortError') {{
              throw new Error('history fetch timed out after ' + timeoutMs + 'ms');
            }}
            throw error;
          }} finally {{
            clearTimeout(timeoutId);
          }}
        }}

        function historyLinesFromText(text) {{
          const lines = (text || '').replace(/\\r?\\n/g, '\\r\\n').replace(/\\r/g, '').split('\\n');
          if (lines.length > 0 && lines[lines.length - 1] === '') {{
            lines.pop();
          }}
          return lines;
        }}

        function fullReplayPayloadForSnapshot(snapshot) {{
          const safeSnapshot = snapshot || {{}};
          const lines = historyLinesFromText(safeSnapshot.history);
          if (lines.length === 0) return '';
          return '\\x1b[H\\x1b[2J\\x1b[3J' + lines.join('\\r\\n') + cursorSeq(safeSnapshot.cursorX, safeSnapshot.cursorY);
        }}

        function scrollTerminalToBottom(term, options) {{
          if (!term) return;
          const shouldRefresh = options && options.refresh === true;
          try {{
            const bufferService = term._core && term._core._bufferService;
            if (bufferService) {{
              bufferService.isUserScrolling = false;
            }}
            if (typeof term.scrollToBottom === 'function') {{
              term.scrollToBottom();
            }}
            const viewportEl = document.querySelector('.xterm-viewport');
            if (viewportEl) {{
              viewportEl.scrollTop = viewportEl.scrollHeight;
            }}
            if (shouldRefresh && typeof term.refresh === 'function') {{
              term.refresh(0, Math.max(0, (term.rows || 1) - 1));
            }}
          }} catch (error) {{
            console.debug('claude-hub scroll-to-bottom failed', error);
          }}
        }}

        function postHistoryRefreshResult(reason, ok, detail, requestId, extras) {{
          try {{
            const target = window.parent && window.parent !== window ? window.parent : window;
            const payload = {{
              type: 'terminal-history-refresh-done',
              tabId: TAB_ID,
              reason: reason || 'manual',
              ok: !!ok,
              error: detail || null,
              requestId: requestId || null,
            }};
            if (extras && extras.deferredRecovery) {{
              payload.deferredRecovery = true;
            }}
            target.postMessage(payload, '*');
          }} catch (error) {{}}
        }}

        function replayHistory(term, fullReplay) {{
          if (!term || replayed || typeof term.write !== 'function') return;
          replayed = true;

          // The history API returns captured terminal content
          // (scrollback + visible screen) from tmux capture-pane. Tokenise
          // line endings in a single pass: the iframe is same-origin with
          // the parent app, so multi-pass regexes over a tens-of-MB
          // historyText would freeze the entire frontend during terminal
          // load (the renderer event loop is shared with the parent).
          const lines = (historyText || '').split(/\\r\\n|\\n|\\r/);
          // Drop the trailing empty string from split('foo\\n') → ['foo','']
          if (lines.length > 0 && lines[lines.length - 1] === '') {{
            lines.pop();
          }}

          if (fullReplay && AGENT_TYPE === 'terminal') {{
            term.__claudeHubReplayBuffering = true;
            runBufferedCaptureHistorySync(term, {{
              fetchLines: INITIAL_HISTORY_LINES,
              releaseOnUserInput: true,
              onUserInput: function() {{
    term.__claudeHubReplayDone = true;
    term.__claudeHubReplayBuffering = false;
    // The interrupted fetch did not establish an authoritative
    // boundary. Keep reconciliation required until the quiet-period
    // refresh succeeds instead of reporting false bootstrap success.
    initialReplayNeedsReconcile = true;
    setupHistoryResync(term);
    scheduleReplayRecoveryAfterInputQuiet();
    flushPendingBootstrapCorrelations();
              }},
              onSuccess: function() {{
    term.__claudeHubReplayDone = true;
    term.__claudeHubReplayBuffering = false;
    initialReplayNeedsReconcile = false;
    setupHistoryResync(term);
    flushPendingBootstrapCorrelations();
              }},
              onError: function(message) {{
    console.debug('claude-hub plain terminal initial replay failed', message);
    term.__claudeHubInitialReplayFailed = true;
    term.__claudeHubReplayDone = true;
    term.__claudeHubReplayBuffering = false;
    setupHistoryResync(term);
    failBootstrapCorrelations(message, bootstrapCorrelationRequestId);
    flushPendingBootstrapCorrelations();
              }},
            }});
            return;
          }}

          if (lines.length === 0) {{
            term.__claudeHubReplayDone = true;
            term.__claudeHubReplayBuffering = false;
            setupHistoryResync(term);
            if (!term.__claudeHubRemoteStableBootstrapInFlight) {{
              tryPostBootstrapForCorrelation(bootstrapCorrelationRequestId);
            }}
            return;
          }}
          if (fullReplay && IS_AGENT_TUI && lines.length < INITIAL_AGENT_REPLAY_MIN_LINES) {{
            // Fresh Claude/Codex/Cursor sessions often produce their logo and
            // startup guidance after the first fast history capture. Replaying
            // that tiny early snapshot and filtering buffered ttyd frames can
            // swallow the real startup screen, so only use initial replay for
            // genuinely long agent histories.
            term.__claudeHubReplaySkippedForShortHistory = true;
            term.__claudeHubReplayDone = true;
            setupHistoryResync(term);
            if (!term.__claudeHubRemoteStableBootstrapInFlight) {{
              tryPostBootstrapForCorrelation(bootstrapCorrelationRequestId);
            }}
            return;
          }}

          // Buffer live writes until history replay completes to prevent
          // history and real-time data from interleaving in the xterm buffer.
          const buffer = [];
          let historyDone = false;
          const originalWrite = term.write;
          const isRemoteAgentReplay = fullReplay && IS_REMOTE_AGENT_TUI;
          const FULL_REPLAY_MIN_HOLD_MS = isRemoteAgentReplay ? 250 : 2500;
          const FULL_REPLAY_QUIET_MS = isRemoteAgentReplay ? 150 : 750;
          const FULL_REPLAY_MAX_HOLD_MS = isRemoteAgentReplay ? 2000 : 8000;
          const FULL_REPLAY_VERIFY_DELAY_MS = 350;
          // Each verify attempt re-pushes the whole multi-MB replayPayload
          // through xterm's parser; cap retries so a buffer-expansion stall
          // cannot freeze the (same-origin) parent for tens of seconds.
          const FULL_REPLAY_VERIFY_ATTEMPTS = 4;
          const FULL_REPLAY_WATCH_MS = 8000;
          const FULL_REPLAY_WATCH_INTERVAL_MS = 100;
          let fullReplayHoldStartedAt = 0;
          let lastBufferedAt = 0;
          let fullReplayHoldTimer = null;
          let fullReplayFinalizing = false;
          let fullReplayVerifyAttempts = 0;
          const replayPayload = '\\x1b[H\\x1b[2J\\x1b[3J' + lines.join('\\r\\n') + cursorSeq(historyCursorX, historyCursorY);
          // replayPlainText is only consulted by isDuplicateInitialFrame,
          // which runs only on agent TUI tabs (filterDuplicateFrames =
          // fullReplay && !AUTO_HISTORY_REPLAY_ENABLED). Skip building it
          // for plain-terminal tabs and cap the agent-TUI version to the
          // visible-screen tail so .indexOf() stays cheap.
          const REPLAY_PLAIN_TEXT_MAX_CHARS = 16384;
          const needsReplayPlainText = fullReplay && !AUTO_HISTORY_REPLAY_ENABLED;
          let replayPlainText = '';
          if (needsReplayPlainText) {{
            const tailLines = lines.length > 200 ? lines.slice(-200) : lines;
            const joined = tailLines.join('\\n');
            replayPlainText = joined.length > REPLAY_PLAIN_TEXT_MAX_CHARS
              ? joined.slice(-REPLAY_PLAIN_TEXT_MAX_CHARS)
              : joined;
          }}
          let replayInputListenerRegistered = false;
          let replayReleasedForInput = false;

          function printableTextFromTerminalData(data) {{
            return String(data || '')
              .replace(/\\x1b\\][^\\x07]*(?:\\x07|\\x1b\\\\)/g, '')
              .replace(/\\x1b\\[[0-?]*[ -/]*[@-~]/g, '')
              .replace(/\\x1b[()][A-Za-z0-9]/g, '')
              .replace(/[\\x00-\\x08\\x0b\\x0c\\x0e-\\x1f\\x7f]/g, '')
              .replace(/\\r/g, '\\n')
              .split('\\n')
              .map(function(line) {{ return line.trim(); }})
              .filter(Boolean)
              .join('\\n');
          }}

          function isDuplicateInitialFrame(data) {{
            const printable = printableTextFromTerminalData(data);
            const compact = printable.replace(/\\s+/g, ' ').trim();
            if (!compact) return true;
            if (compact.length < 12) return false;
            if (replayPlainText.indexOf(printable) >= 0 || replayPlainText.indexOf(compact) >= 0) {{
              return true;
            }}
            const fragments = printable.split('\\n').map(function(line) {{
              return line.trim();
            }}).filter(Boolean);
            return fragments.length > 0 && fragments.every(function(fragment) {{
              return fragment.length < 12 || replayPlainText.indexOf(fragment) >= 0;
            }});
          }}

          term.write = function(data, cb) {{
            if (historyDone) {{
              return originalWrite.call(term, data, cb);
            }}
            if (fullReplay) {{
              lastBufferedAt = Date.now();
            }}
            buffer.push({{ data, cb }});
            return undefined;
          }};
          term.__claudeHubReplayBuffering = true;

          function removeReplayInputListener() {{
            if (!replayInputListenerRegistered) return;
            replayInputListenerRegistered = false;
            const index = terminalUserInputListeners.indexOf(releaseReplayForUserInput);
            if (index >= 0) {{
              terminalUserInputListeners.splice(index, 1);
            }}
          }}

          function scheduleReplayRecoveryAfterInputQuiet() {{
            if (!fullReplay || !AUTO_HISTORY_REPLAY_ENABLED) return;
            setTimeout(function waitForReplayRecoveryQuiet() {{
              if (hasRecentUserInput()) {{
    setTimeout(waitForReplayRecoveryQuiet, userInputQuietDelayMs() + 100);
    return;
              }}
              refreshHistoryWhenReady({{ reason: 'post-replay-input-quiet', scrollToBottom: true }}, 50);
            }}, userInputQuietDelayMs() + 100);
          }}

          function releaseReplayForUserInput() {{
            if (historyDone || !fullReplay) return;
            replayReleasedForInput = true;
            fullReplayFinalizing = true;
            // Buffered ttyd frames predate this keystroke and can take long
            // enough to render that the shell echo feels blocked. The replay
            // snapshot is already visible, so acknowledge those stale writes
            // without rendering them, restore live term.write immediately,
            // and reconcile from tmux after the input quiet period.
            flushBuffer(true);
            scheduleReplayRecoveryAfterInputQuiet();
          }}

          function flushReplayBufferThen(callback, discardWrites) {{
            const filterDuplicateFrames = fullReplay && !AUTO_HISTORY_REPLAY_ENABLED;
            function drainBatch() {{
              if (buffer.length === 0) {{
    if (typeof callback === 'function') callback();
    return;
              }}
              let pending = buffer.length;
              const batch = buffer.splice(0, buffer.length);
              batch.forEach(function(item) {{
    function onItemDone() {{
      try {{
        if (typeof item.cb === 'function') item.cb();
      }} finally {{
        pending--;
        if (pending === 0) drainBatch();
      }}
    }}
    if (discardWrites || (filterDuplicateFrames && isDuplicateInitialFrame(item.data))) {{
      onItemDone();
      return;
    }}
    originalWrite.call(term, item.data, onItemDone);
              }});
            }}
            drainBatch();
          }}

          function flushBuffer(discardWrites) {{
            if (historyDone) return;
            if (fullReplayHoldTimer) {{
              clearTimeout(fullReplayHoldTimer);
              fullReplayHoldTimer = null;
            }}
            clearTimeout(safetyTimer);
            historyDone = true;
            flushReplayBufferThen(function() {{
              removeReplayInputListener();
              term.write = originalWrite;
              term.__claudeHubReplayDone = true;
              term.__claudeHubReplayBuffering = false;
              // Input-triggered release deliberately discarded buffered ttyd
              // frames. Keep bootstrap gated until an authoritative tmux
              // refresh succeeds; normal replay completion is already whole.
              initialReplayNeedsReconcile = replayReleasedForInput;
              setupHistoryResync(term);
              startPostReplayWatch();
              flushPendingBootstrapCorrelations();
              if (fullReplay && AUTO_HISTORY_REPLAY_ENABLED && !replayReleasedForInput) {{
    setTimeout(function() {{
      refreshHistoryWhenReady({{ reason: 'post-replay-flush', scrollToBottom: true }}, 50);
    }}, 0);
              }}
            }}, discardWrites === true);
          }}

          function hasExpectedReplayBuffer() {{
            if (!fullReplay) return true;
            const active = term.buffer && term.buffer.active;
            if (!active) return true;
            const rows = term.rows || 24;
            if (lines.length > rows && active.baseY <= 0) return false;
            return active.length >= Math.min(lines.length, rows);
          }}

          function startPostReplayWatch() {{
            if (!fullReplay) return;
            const watchUntil = Date.now() + FULL_REPLAY_WATCH_MS;
            const watchInputGeneration = userInputGeneration;
            function watch() {{
              if (!historyDone) return;
              if (userInputGeneration !== watchInputGeneration) return;
              if (!hasExpectedReplayBuffer() && AUTO_HISTORY_REPLAY_ENABLED && !hasRecentUserInput()) {{
    // Rewriting the snapshot we captured at replay time would
    // roll back any live ws data that arrived during the hold.
    // Refetch tmux for the current state instead.
    refreshHistoryWhenReady({{ reason: 'post-replay-watch', scrollToBottom: false }}, 0);
              }}
              if (Date.now() < watchUntil) {{
    setTimeout(watch, FULL_REPLAY_WATCH_INTERVAL_MS);
              }}
            }}
            setTimeout(watch, FULL_REPLAY_WATCH_INTERVAL_MS);
          }}

          function verifyFullReplayBeforeDone() {{
            setTimeout(function() {{
              if (hasExpectedReplayBuffer() || fullReplayVerifyAttempts >= FULL_REPLAY_VERIFY_ATTEMPTS) {{
    flushBuffer();
    return;
              }}
              fullReplayVerifyAttempts++;
              originalWrite.call(term, replayPayload, verifyFullReplayBeforeDone);
            }}, FULL_REPLAY_VERIFY_DELAY_MS);
          }}

          function finishFullReplay() {{
            if (!fullReplay) {{
              flushBuffer();
              return;
            }}
            if (fullReplayFinalizing) return;
            fullReplayFinalizing = true;
            if (fullReplayHoldTimer) {{
              clearTimeout(fullReplayHoldTimer);
              fullReplayHoldTimer = null;
            }}
            // A final replay right before releasing the buffer overwrites any
            // late ttyd initial-screen frames that arrived during the hold.
            // On Linux CI, a later resize/initial-frame burst can still
            // collapse scrollback; verify the xterm buffer before setting
            // the public replay-done flag used by E2E readiness checks.
            originalWrite.call(term, replayPayload, verifyFullReplayBeforeDone);
          }}

          function finishFullReplayWhenQuiet() {{
            if (!fullReplay) {{
              flushBuffer();
              return;
            }}
            const now = Date.now();
            const elapsed = now - fullReplayHoldStartedAt;
            const quietFor = lastBufferedAt ? now - lastBufferedAt : elapsed;
            if (
              elapsed >= FULL_REPLAY_MAX_HOLD_MS ||
              (elapsed >= FULL_REPLAY_MIN_HOLD_MS && quietFor >= FULL_REPLAY_QUIET_MS)
            ) {{
              finishFullReplay();
              return;
            }}
            const waitMs = Math.max(
              50,
              Math.min(
    FULL_REPLAY_MAX_HOLD_MS - elapsed,
    Math.max(FULL_REPLAY_MIN_HOLD_MS - elapsed, FULL_REPLAY_QUIET_MS - quietFor)
              )
            );
            fullReplayHoldTimer = setTimeout(finishFullReplayWhenQuiet, waitMs);
          }}

          // Safety timeout: release buffer if callbacks never fire.
          const safetyTimer = setTimeout(function() {{
            if (fullReplay && !fullReplayFinalizing) {{
              finishFullReplay();
              return;
            }}
            flushBuffer();
          }}, FULL_REPLAY_MAX_HOLD_MS + 8000);

          if (fullReplay) {{
            replayInputListenerRegistered = true;
            terminalUserInputListeners.push(releaseReplayForUserInput);
          }}

          if (fullReplay) {{
            // Phase B: term.open() was already called by ttyd and the
            // visible screen is already rendered.  Clear the entire buffer
            // (screen + scrollback) and write the full terminal content
            // from scratch.  The last `rows` lines will land on the
            // visible screen; the rest becomes scrollback.
            // The \\x1b[3J clears scrollback, \\x1b[H\\x1b[2J clears screen.
            originalWrite.call(term, replayPayload, function() {{
              // ttyd can still deliver its initial screen payload after
              // xterm accepts the replay write, especially under the Linux
              // CI binary. Keep term.write buffered until that stream has
              // gone quiet so late duplicate initial frames cannot collapse
              // reconstructed scrollback back to only the visible screen rows.
              fullReplayHoldStartedAt = Date.now();
              finishFullReplayWhenQuiet();
            }});
          }} else {{
            // Phase A: term.open() has NOT been called yet (our hook will
            // fire it).  Only scrollback lines need to be written; the
            // visible screen will be delivered by ttyd's WebSocket.
            // After writing scrollback, use Scroll Up (SU) to push the
            // bottom `rows` lines (which land on the visible screen) into
            // scrollback so the visible screen is left blank for ttyd.
            var scrollUpSeq = '\\x1b[' + (term.rows || 24) + 'S';
            originalWrite.call(term, lines.join('\\r\\n') + '\\r\\n' + scrollUpSeq, function() {{
              flushBuffer();
            }});
          }}
        }}

        function hookTerm(term) {{
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
