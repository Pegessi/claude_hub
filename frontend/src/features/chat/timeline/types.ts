import type { SubagentView } from '@/utils/subagentTool'

export interface TimelineTool {
  key: string
  callId: string | null
  name: string
  status: 'running' | 'completed' | 'failed' | 'cancelled'
  argsText: string
  resultText: string
  /** Local path when this call is an agent image view (Codex/TraeX
   *  ``view_image`` or a Claude ``Read`` of an image). When set, the reducer
   *  emits a dedicated ``agent_image`` part instead of grouping the call in a
   *  generic tool card. The endpoint serves the bytes after validating that
   *  the path stays inside the session cwd and really is an image. */
  imagePath?: string
  /** Present only for confirmed sub-agent-spawn tools (Claude ``Agent``,
   *  Cursor ``Task``, TraeX ``spawnAgent``); the renderer gives these their own
   *  card instead of the generic grouped tool block. */
  subagent?: SubagentView
}

export interface TimelineQuestionOption {
  id: string
  label: string
  description?: string
}

export interface TimelineQuestion {
  id: string
  prompt: string
  allowMultiple: boolean
  isSecret?: boolean
  options: TimelineQuestionOption[]
}

export interface TimelineApproval {
  key: string
  callId: string | null
  kind: string
  title: string | null
  questions: TimelineQuestion[]
  resolved: boolean
}

/** Durable attachment descriptor carried by ``turn_started``.
 *
 *  The durable attachment cache stores only the browser-generated bounded
 *  preview bytes (max edge 1024px, max 512 KiB). The original image bytes are
 *  transient provider input — they are forwarded to the model and never
 *  persisted (the Codex native provider stages them under the runtime temp
 *  dir and deletes them when the turn ends). The user bubble fetches the
 *  persisted preview from the scoped attachment GET endpoint using ``id``.
 *
 *  ``id`` may be ``null`` for a deliberate no-preview placeholder when the
 *  client did not supply a bounded preview. Eviction keeps the opaque id in
 *  history; the scoped GET then returns 410 and the template replaces the
 *  failed image with a stable placeholder. */
export interface TimelineAttachment {
  id: string | null
  mime_type: string
  bytes: number
  width?: number
  height?: number
}

export type TimelineTurnOrigin = 'feishu' | 'web' | 'unknown'

export interface TimelineFeishuProvenance {
  appId: string
  botName: string | null
  botAvatarUrl: string | null
  chatId: string
  messageId: string
  senderOpenId: string
}

/** One row rendered inside a nested sub-agent thread group (``subthread``).
 *
 *  This is the child-thread analogue of {@link TimelinePart}, deliberately
 *  narrower: a child emits thinking, prose, tool calls, and the directives the
 *  main agent posts to it (``sendInput``). Control-plane rows (approvals,
 *  errors, plan statuses) are not produced by the collab child in captured
 *  streams and stay on the main stream if they ever arrive. */
export type TimelineSubthreadPart =
  | { kind: 'thinking'; key: string; text: string }
  | { kind: 'text'; key: string; text: string; at: string }
  | { kind: 'tool_group'; key: string; tools: TimelineTool[] }
  | { kind: 'agent_image'; key: string; tool: TimelineTool; path: string }
  // A ``sendInput`` directive the main agent posted to this child thread.
  | { kind: 'instruction'; key: string; tool: TimelineTool }
  // A child's in-flight process status (e.g. "Thinking…"). The stable
  // messageId + snapshot lets a later update (or the turn's terminal
  // finalization) replace it in place rather than appending a second row.
  | { kind: 'status'; key: string; text: string; messageId?: string | null; providerStatus?: string; isError?: boolean }

export type TimelineSubagentLifecycle =
  | 'unknown'
  | 'active'
  | 'completed'
  | 'failed'
  | 'cancelled'

export interface TimelineSubthread {
  key: string
  threadId: string
  parts: TimelineSubthreadPart[]
  /** The correlated native spawn call. Its status is dispatch status only. */
  launchTool: TimelineTool | null
  /** Provider-observed child-turn lifecycle; never inferred from parent state. */
  lifecycleStatus: TimelineSubagentLifecycle
  /** Child-local revision so sibling stream updates can reuse display projections. */
  renderRevision: number
}

export type TimelinePart =
  | { kind: 'thinking'; key: string; text: string }
  // Plan proposals are the delivery in Plan mode; progress checklists remain
  // part of the working process. Provider snapshots replace by message id.
  //
  // ``at`` is when this message began streaming — the timestamp the transcript
  // shows beside the message's actions. Only text parts carry one: they are
  // what the transcript presents per-message, while thinking and tool parts are
  // folded into a process line that reports the turn's elapsed time instead.
  | { kind: 'text'; key: string; text: string; at: string; fromPlan?: boolean; messageId?: string; planKind?: 'proposal' | 'progress' }
  | { kind: 'tool'; key: string; tool: TimelineTool }
  | { kind: 'tool_group'; key: string; tools: TimelineTool[] }
  // A confirmed sub-agent-spawn call, split out of the ordinary tool groups so
  // the pane can render a dedicated Codex-style sub-agent card. Carries the
  // same TimelineTool (status/result mutate in place) plus the parsed view.
  | { kind: 'subagent'; key: string; tool: TimelineTool }
  // One expandable native-subagent region per parent turn. Child identity is
  // always the complete provider thread id; each row retains its full process.
  | { kind: 'subagents'; key: string; threads: TimelineSubthread[] }
  // An agent-produced local image (view_image / image Read), split out of the
  // tool groups so the pane renders the actual picture (click → lightbox)
  // instead of a raw path string. ``tool`` carries the live status; ``path``
  // is the filesystem path the scoped agent-image endpoint serves.
  | { kind: 'agent_image'; key: string; tool: TimelineTool; path: string }
  | { kind: 'approval'; key: string; approval: TimelineApproval }
  // ``detail`` is the provider's verbatim failure payload, kept beside the
  // message because an adapter's summary of it is necessarily lossy.
  | { kind: 'error'; key: string; message: string; detail?: string }
  | { kind: 'status'; key: string; text: string; messageId?: string | null; providerStatus?: string }
  // Synthetic part produced by ``foldTurnParts`` — never emitted by the
  // reducer. It is the folded working region's header: the toggle that both
  // reveals and hides the detail beneath it.
  | { kind: 'process'; key: string; meta: string; expanded: boolean }

export interface TimelineTurn {
  key: string
  turnId: string | null
  userText: string
  /** Explicit server-authored message source. Missing legacy metadata stays unknown. */
  origin: TimelineTurnOrigin
  /** Verified Feishu callback identity, present only when origin is feishu. */
  feishu: TimelineFeishuProvenance | null
  /** Mode when this turn began, not the composer's current selection. */
  mode: 'default' | 'plan' | null
  /** Durable attachment descriptors surfaced by ``turn_started``.
   *  Each entry carries the opaque attachment id, mime type, byte size,
   *  and optional pixel dimensions — never raw bytes or local paths. The
   *  user bubble resolves the preview via the scoped attachment GET. */
  attachments: TimelineAttachment[]
  /** Ordered sequence of thinking/text/tool/error/status parts as they arrived. */
  parts: TimelinePart[]
  // Compatibility aggregates kept for callers/tests that read the flat
  // buckets. ``parts`` is the authoritative render order; these are derived
  // views over it.
  assistantText: string
  thinkingText: string
  tools: TimelineTool[]
  approvals: TimelineApproval[]
  completed: boolean
  completionStatus: string | null
  /** Wall-clock of ``turn_started`` / ``turn_completed``, used to label the
   *  folded process with how long the turn took. ``null`` until the matching
   *  event arrives; a turn whose spans are not real elapsed time (history
   *  replayed from a provider transcript stamps every event with the import
   *  time) is filtered by ``turnElapsedMs`` rather than shown as ``0s``. */
  startedAt: string | null
  completedAt: string | null
  errors: { key: string; message: string }[]
  statuses: { key: string; text: string }[]
  /**
   * Monotonically increasing render revision. Incremented only when an
   * applied event visibly mutates this turn (text/thinking/tool/status/
   * error/completion/user summary). Events that are no-ops (empty text,
   * exact multi-chunk replay, duplicate tool) do not advance the revision.
   *
   * ``StructuredPane`` uses ``v-memo="[turn.renderRevision]"`` on each
   * ``.structured-turn`` so Vue skips re-rendering completed historical
   * turns whose revision has not changed; only the active turn rebuilds.
   */
  renderRevision: number
}
