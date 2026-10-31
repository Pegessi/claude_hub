"""Dynamic prelude for terminal bootstrap injection."""

import json


def render_bootstrap_prelude(
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
        const IS_AGENT_TUI = AGENT_TYPE === 'claude' || AGENT_TYPE === 'codex' || AGENT_TYPE === 'traex' || AGENT_TYPE === 'cursor' || AGENT_TYPE === 'antigravity';
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

"""
