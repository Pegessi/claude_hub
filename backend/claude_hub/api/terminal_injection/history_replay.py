"""Terminal history loading and replay bootstrap fragment."""


def render_history_replay() -> str:
    return f"""        let currentTerm = undefined;
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

"""
