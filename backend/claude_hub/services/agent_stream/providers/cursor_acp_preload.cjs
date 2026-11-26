"use strict";
// Cursor CLI 2026.11.11's ACP session runner (./src/acp/agent-session.ts)
// wires onConnectionStateChange and onErrorNotRetried into agentClient.run's
// runOptions, but omits enableAgentRetries: true (which headless --print,
// interactive TUI, and subagent runners all set). Without it,
// agentClient.run evaluates `retriesEnabled: p.enableAgentRetries ?? false`
// and aborts the turn on attempt=0 on any transient RetriableError (such as
// upstream [resource_exhausted] 429 rate-limit blips or HTTP/2 stream resets)
// instead of resuming from the latest SQLite checkpoint via resumeAction.
//
// Additionally:
// 1. ModelManager.prototype.mapModelToParameterizedSelection in 1623.index.js
//    maps legacy variant slugs (e.g. example-model-high) to the base model's
//    saved/default parameters instead of consulting
//    mapInitialModelVariantToParameterizedSelection for the variant's exact
//    parameterValues.
// 2. ./src/acp/agent-store.ts and ./src/acp/session-resources.ts only enable
//    isRunEverything when CLI --yolo (options.force) is passed, ignoring
//    `approvalMode: "unrestricted"` in ~/.cursor/cli-config.json (which
//    headless --print and TUI honor).
//
// We intercept webpack's CommonJS chunk loader via Module._load so
// Object.prototype is never modified.

(function () {
  // Scrub only this preload token from NODE_OPTIONS immediately so child
  // processes spawned by Cursor's Shell tool or MCP servers never inherit it,
  // while preserving any existing flags (including quoted paths with spaces).
  const selfPath = __filename;
  const rawOpts = process.env.NODE_OPTIONS;
  if (typeof rawOpts === "string" && rawOpts.includes(selfPath)) {
    const escaped = selfPath.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const tokenRe = new RegExp(
      `(?:^|\\s+)--require(?:=|\\s+)(?:"${escaped}"|'${escaped}'|${escaped})(?=\\s|$)`,
      "g"
    );
    const cleaned = rawOpts.replace(tokenRe, "").trim();
    if (cleaned) {
      process.env.NODE_OPTIONS = cleaned;
    } else {
      delete process.env.NODE_OPTIONS;
    }
  }

  function patchAcpRunOptions(opts) {
    if (opts && typeof opts === "object") {
      if (opts.enableAgentRetries === undefined) {
        opts.enableAgentRetries = true;
      }
      // Keep the SDK's separate server/transport budgets. maxRetries imposes
      // a shared cap and can exhaust transport recovery on earlier server errors.
    }
    return opts;
  }

  // agentClient is shared across ACP sessions. Associate each run with its
  // actual store rather than a process-global "current session" callback.
  const activeStores = new WeakMap();

  function errorDiagnostic(error) {
    const detail = error && error.displayInfo;
    const result = { event: "error" };
    for (const key of ["kind", "action", "requestId"]) {
      const value = error && error[key];
      if (typeof value === "string") result[key] = value.slice(0, 160);
    }
    for (const key of ["connectCode", "errorCode", "inferenceRequestErrorType"]) {
      const value = detail && detail[key];
      if (typeof value === "number" || typeof value === "string") {
        result[key] = typeof value === "string" ? value.slice(0, 160) : value;
      }
    }
    // Hub applies its normal diagnostic redaction before using this text.
    // Never serialize the error object: causes/headers can contain credentials.
    if (error && typeof error.message === "string") {
      result.message = error.message.slice(0, 1500);
    }
    return result;
  }

  async function reportRuntime(context, payload) {
    if (!context || !context.active) return;
    const session = context.session;
    if (!session.connection || typeof session.connection.extNotification !== "function") return;
    try {
      await session.connection.extNotification("_claude_hub/cursor_runtime", {
        sessionId: session.sessionId,
        ...payload,
      });
    } catch {
      // Reporting must not replace the provider result or break its retries.
    }
  }

  function patchAgentClient(ac) {
    if (!ac || typeof ac.run !== "function" || ac.run.__claudeHubPatched) {
      return;
    }
    const origRun = ac.run;
    const patchedRun = async function (...runArgs) {
      const context = activeStores.get(runArgs[8]);
      if (runArgs.length >= 11) {
        const options = patchAcpRunOptions({ ...runArgs[10] });
        const onConnectionStateChange = options.onConnectionStateChange;
        options.onConnectionStateChange = function (state) {
          if (state && ["reconnecting", "connected"].includes(state.state)) {
            const payload = { event: "connection", state: state.state };
            if (Number.isSafeInteger(state.attempt) && state.attempt > 0) {
              payload.attempt = state.attempt;
            }
            // Only real SDK progress, never a timer-generated heartbeat.
            void reportRuntime(context, payload);
          }
          return onConnectionStateChange?.apply(this, arguments);
        };
        runArgs[10] = options;
      }
      try {
        const result = await origRun.apply(this, runArgs);
        if (context?.retriableError) {
          context.retriableError = undefined;
          if (!runArgs[0]?.canceled && !runArgs[0]?.signal?.aborted) {
            // A successful background followup may emit only turnEnded, which
            // ACP does not forward. Resolve its earlier inline error explicitly.
            await reportRuntime(context, { event: "recovered" });
          }
        }
        return result;
      } catch (error) {
        // onErrorNotRetried only covers attempt=0. Catch also covers exhausted
        // retries, before ACP replaces ActionRequiredError with "sign in".
        if (context) context.retriableError = undefined;
        if (!runArgs[0]?.canceled && !runArgs[0]?.signal?.aborted) {
          const diagnostic = errorDiagnostic(error);
          if (context && diagnostic.kind === "RetriableError") {
            // processPrompt can run background-completion followups in finally.
            // Only a later successful run, not "connected", resolves this error.
            context.retriableError = diagnostic;
          } else {
            await reportRuntime(context, diagnostic);
          }
        }
        throw error;
      }
    };
    patchedRun.__claudeHubPatched = true;
    ac.run = patchedRun;
  }

  function patchModelManager(mm) {
    if (
      !mm ||
      typeof mm.mapModelToParameterizedSelection !== "function" ||
      typeof mm.mapInitialModelVariantToParameterizedSelection !== "function" ||
      mm.mapModelToParameterizedSelection.__claudeHubPatched
    ) {
      return;
    }
    const origMap = mm.mapModelToParameterizedSelection;
    const patchedMap = function (modelId, configProvider) {
      if (
        typeof modelId === "string" &&
        this.parameterizedModelMap &&
        typeof this.parameterizedModelMap.has === "function" &&
        !this.parameterizedModelMap.has(modelId)
      ) {
        const exactVariant = this.mapInitialModelVariantToParameterizedSelection(modelId);
        if (exactVariant) {
          return exactVariant;
        }
      }
      return origMap.apply(this, arguments);
    };
    patchedMap.__claudeHubPatched = true;
    mm.mapModelToParameterizedSelection = patchedMap;
  }

  function isUnrestrictedConfig(sharedServices) {
    try {
      const cfg =
        sharedServices &&
        sharedServices.configProvider &&
        typeof sharedServices.configProvider.get === "function"
          ? sharedServices.configProvider.get()
          : null;
      return Boolean(cfg && cfg.approvalMode === "unrestricted");
    } catch {
      return false;
    }
  }

  function syncRunEverythingMetadata(sessionInstance) {
    try {
      const store = sessionInstance && sessionInstance.agentStore;
      if (!store || typeof store.getMetadata !== "function" || typeof store.setMetadata !== "function") {
        return;
      }
      const mode = store.getMetadata("mode");
      if (mode === "plan" || mode === "ask") {
        return;
      }
      if (isUnrestrictedConfig(sessionInstance.sharedServices)) {
        if (store.getMetadata("isRunEverything") !== true) {
          store.setMetadata("isRunEverything", true);
        }
      }
    } catch {
      // Best-effort metadata sync.
    }
  }

  function patchAgentSessionClass(AgentSession) {
    if (!AgentSession || !AgentSession.prototype || AgentSession.__claudeHubPatched) {
      return;
    }
    AgentSession.__claudeHubPatched = true;
    const origProcessPrompt = AgentSession.prototype.processPrompt;
    if (typeof origProcessPrompt === "function") {
      AgentSession.prototype.processPrompt = async function (...args) {
        if (this && this.sharedServices) {
          patchAgentClient(this.sharedServices.agentClient);
          patchModelManager(this.sharedServices.modelManager);
        }
        syncRunEverythingMetadata(this);
        const context = { session: this, active: true };
        const store = this.agentStore;
        if (store && typeof store === "object") activeStores.set(store, context);
        try {
          return await origProcessPrompt.apply(this, args);
        } finally {
          const token = args[1];
          if (context.retriableError && !token?.canceled && !token?.signal?.aborted) {
            await reportRuntime(context, context.retriableError);
          }
          context.active = false;
          if (store && activeStores.get(store) === context) activeStores.delete(store);
        }
      };
    }
  }

  function patchCursorAcpAgentClass(CursorAcpAgent) {
    if (!CursorAcpAgent || !CursorAcpAgent.prototype || CursorAcpAgent.__claudeHubPatched) {
      return;
    }
    CursorAcpAgent.__claudeHubPatched = true;
    for (const method of ["newSession", "loadSession", "setSessionModel"]) {
      const orig = CursorAcpAgent.prototype[method];
      if (typeof orig === "function") {
        CursorAcpAgent.prototype[method] = async function (...args) {
          if (this && this.sharedServices) {
            patchAgentClient(this.sharedServices.agentClient);
            patchModelManager(this.sharedServices.modelManager);
            if (
              isUnrestrictedConfig(this.sharedServices) &&
              this.options &&
              typeof this.options === "object" &&
              !this.options.force
            ) {
              this.options = Object.assign({}, this.options, { force: true });
            }
          }
          return orig.apply(this, args);
        };
      }
    }
  }

  Object.defineProperty(globalThis, "__claudeHubCursorAcpPreload", {
    value: {
      patchAcpRunOptions,
      patchAgentClient,
      patchModelManager,
      patchAgentSessionClass,
      patchCursorAcpAgentClass,
      syncRunEverythingMetadata,
    },
    configurable: true,
    enumerable: false,
    writable: false,
  });

  if (!Array.isArray(process.argv) || !process.argv.includes("acp")) {
    return;
  }

  const Module = require("node:module");
  const origLoad = Module._load;
  if (typeof origLoad === "function" && !origLoad.__claudeHubPatched) {
    const patchedLoad = function (request, parent, isMain) {
      const loaded = origLoad.apply(this, arguments);
      if (loaded && typeof loaded === "object" && loaded.modules) {
        const mods = loaded.modules;
        if (
          typeof mods["./src/acp/agent-session.ts"] === "function" &&
          !mods["./src/acp/agent-session.ts"].__claudeHubPatched
        ) {
          const origFactory = mods["./src/acp/agent-session.ts"];
          const wrappedFactory = function (e, t, s) {
            const wrappedS = Object.assign(
              function (id) {
                return s(id);
              },
              s,
              {
                d(target, def) {
                  if (def && typeof def.m === "function") {
                    const origGetter = def.m;
                    def = Object.assign({}, def, {
                      m() {
                        const cls = origGetter();
                        patchAgentSessionClass(cls);
                        return cls;
                      },
                    });
                  }
                  return s.d(target, def);
                },
              }
            );
            return origFactory.call(this, e, t, wrappedS);
          };
          wrappedFactory.__claudeHubPatched = true;
          mods["./src/acp/agent-session.ts"] = wrappedFactory;
        }
        if (
          typeof mods["./src/acp/cursor-acp-agent.ts"] === "function" &&
          !mods["./src/acp/cursor-acp-agent.ts"].__claudeHubPatched
        ) {
          const origAgentFactory = mods["./src/acp/cursor-acp-agent.ts"];
          const wrappedAgentFactory = function (e, t, s) {
            const wrappedS = Object.assign(
              function (id) {
                return s(id);
              },
              s,
              {
                d(target, def) {
                  if (def && typeof def.o === "function") {
                    const origGetter = def.o;
                    def = Object.assign({}, def, {
                      o() {
                        const cls = origGetter();
                        patchCursorAcpAgentClass(cls);
                        return cls;
                      },
                    });
                  }
                  return s.d(target, def);
                },
              }
            );
            return origAgentFactory.call(this, e, t, wrappedS);
          };
          wrappedAgentFactory.__claudeHubPatched = true;
          mods["./src/acp/cursor-acp-agent.ts"] = wrappedAgentFactory;
        }
      }
      return loaded;
    };
    patchedLoad.__claudeHubPatched = true;
    Module._load = patchedLoad;
  }
})();
