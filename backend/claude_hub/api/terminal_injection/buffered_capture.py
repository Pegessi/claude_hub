"""Buffered terminal capture bootstrap fragment."""


def render_buffered_capture() -> str:
    return f"""        function runBufferedCaptureHistorySync(term, options) {{
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

"""
