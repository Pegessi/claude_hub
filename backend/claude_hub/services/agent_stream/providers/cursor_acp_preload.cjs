"use strict";
// Cursor CLI 2026.11.11's ACP session runner (./src/acp/agent-session.ts)
// wires onConnectionStateChange and onErrorNotRetried into agentClient.run's
// runOptions, but omits enableAgentRetries: true (which headless --print,
// interactive TUI, and subagent runners all set). Without it,
// agentClient.run evaluates `retriesEnabled: p.enableAgentRetries ?? false`
// and aborts the turn on attempt=0 on any transient RetriableError (such as
// upstream [resource_exhausted] 429 rate-limit blips or HTTP/2 stream resets)
// instead of resuming from the latest SQLite checkpoint via resumeAction.

(function () {
  // Scrub this preload from NODE_OPTIONS immediately so child processes
  // spawned by Cursor's Shell tool or MCP servers never inherit it.
  const selfPath = __filename;
  const rawOpts = process.env.NODE_OPTIONS;
  if (typeof rawOpts === "string" && rawOpts.includes(selfPath)) {
    const escaped = selfPath.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const cleaned = rawOpts
      .replace(new RegExp(`--require(?:=|\\s+)"?${escaped}"?`, "g"), "")
      .trim()
      .replace(/\s+/g, " ");
    if (cleaned) {
      process.env.NODE_OPTIONS = cleaned;
    } else {
      delete process.env.NODE_OPTIONS;
    }
  }

  if (!Array.isArray(process.argv) || !process.argv.includes("acp")) {
    return;
  }

  function isAcpAgentClientRunOptions(obj) {
    return (
      obj !== null &&
      typeof obj === "object" &&
      typeof obj.conversationId === "string" &&
      typeof obj.onConnectionStateChange === "function" &&
      typeof obj.onErrorNotRetried === "function"
    );
  }

  if (!Object.prototype.hasOwnProperty("enableAgentRetries")) {
    Object.defineProperty(Object.prototype, "enableAgentRetries", {
      configurable: true,
      enumerable: false,
      get() {
        return isAcpAgentClientRunOptions(this) ? true : undefined;
      },
      set(v) {
        Object.defineProperty(this, "enableAgentRetries", {
          value: v,
          writable: true,
          configurable: true,
          enumerable: true,
        });
      },
    });
  }

  if (!Object.prototype.hasOwnProperty("maxRetries")) {
    Object.defineProperty(Object.prototype, "maxRetries", {
      configurable: true,
      enumerable: false,
      get() {
        return isAcpAgentClientRunOptions(this) ? 8 : undefined;
      },
      set(v) {
        Object.defineProperty(this, "maxRetries", {
          value: v,
          writable: true,
          configurable: true,
          enumerable: true,
        });
      },
    });
  }
})();
