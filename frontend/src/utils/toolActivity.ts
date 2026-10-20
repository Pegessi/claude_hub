import type { TimelineTool } from './agentStreamTimeline'

/**
 * Activity presentation for tool calls: one readable line per call
 * ("Ran `pnpm test`", "Edited `src/app.ts`") instead of a raw tool name and a
 * JSON blob. Providers name the same action differently (Claude ``Bash``,
 * Codex ``exec_command``, Cursor ``Shell``), so names are classified loosely and
 * anything unknown falls back to the tool name itself.
 */
export type ToolActivityKind =
  | 'command'
  | 'read'
  | 'edit'
  | 'write'
  | 'search'
  | 'list'
  | 'web'
  | 'plan'
  | 'mcp'
  | 'other'

export interface ToolActivity {
  kind: ToolActivityKind
  verb: string
  /** Short target (command, path, query); empty when the call has none. */
  target: string
  /** Full shell command for terminal-style rendering of command calls. */
  command?: string
  /** Line diff for edit calls, when the arguments carry one. */
  diff?: ToolDiffLine[]
}

export interface ToolDiffLine {
  sign: '+' | '-' | ' ' | '@'
  text: string
}

/** Stroke-only 16px glyphs; rendered with ``fill: none; stroke: currentColor``. */
export const ACTIVITY_ICONS: Record<ToolActivityKind, string> = {
  command: 'M2.5 4.5 6 8l-3.5 3.5M8 12h5.5',
  read: 'M4 1.75h5l3 3v9.5H4zM9 1.75v3h3M6 8h4M6 10.5h4',
  edit: 'M10.5 2.5l3 3L6 13H3v-3zM9 4l3 3',
  write: 'M4 1.75h5l3 3v9.5H4zM9 1.75v3h3M8 7.5v5M5.5 10h5',
  search: 'M7 2.5a4.5 4.5 0 1 0 0 9 4.5 4.5 0 0 0 0-9zM10.3 10.3 14 14',
  list: 'M1.75 3.75h4.5l1.5 1.5h6.5v7.5H1.75z',
  web: 'M8 1.75a6.25 6.25 0 1 0 0 12.5 6.25 6.25 0 0 0 0-12.5zM1.75 8h12.5M8 1.75c-2.4 2.4-2.4 10.1 0 12.5M8 1.75c2.4 2.4 2.4 10.1 0 12.5',
  plan: 'M2.5 4l1.5 1.5L6.5 3M8.5 4.5h5M2.5 10l1.5 1.5L6.5 9M8.5 10.5h5',
  mcp: 'M5.5 1.75v3M10.5 1.75v3M3.5 4.75h9v3a4.5 4.5 0 0 1-9 0zM8 12.25v2',
  other: 'M8 5.25a2.75 2.75 0 1 0 0 5.5 2.75 2.75 0 0 0 0-5.5zM8 1.75v1.5M8 12.75v1.5M1.75 8h1.5M12.75 8h1.5M3.6 3.6l1 1M11.4 11.4l1 1M3.6 12.4l1-1M11.4 4.6l1-1',
}

const TARGET_LIMIT = 120
const LARGE_ARGS = 64 * 1024
const LARGE_ARGS_STEP = 32 * 1024
const DIFF_LINE_LIMIT = 400

const KIND_BY_NAME: Record<string, ToolActivityKind> = {
  bash: 'command',
  shell: 'command',
  exec_command: 'command',
  local_shell: 'command',
  run_terminal_cmd: 'command',
  terminal: 'command',
  command: 'command',
  shell_command: 'command',
  unified_exec: 'command',
  'container.exec': 'command',
  'functions.exec_command': 'command',
  write_stdin: 'command',
  bashoutput: 'command',
  'functions.shell': 'command',
  read: 'read',
  read_file: 'read',
  view: 'read',
  notebookread: 'read',
  view_image: 'read',
  edit: 'edit',
  multiedit: 'edit',
  apply_patch: 'edit',
  str_replace: 'edit',
  str_replace_editor: 'edit',
  notebookedit: 'edit',
  search_replace: 'edit',
  write: 'write',
  write_file: 'write',
  create_file: 'write',
  delete: 'write',
  delete_file: 'write',
  grep: 'search',
  search: 'search',
  codebase_search: 'search',
  file_search: 'search',
  glob: 'list',
  ls: 'list',
  list: 'list',
  list_dir: 'list',
  list_directory: 'list',
  webfetch: 'web',
  web_fetch: 'web',
  websearch: 'web',
  web_search: 'web',
  todowrite: 'plan',
  update_plan: 'plan',
  updatetodos: 'plan',
  todo_write: 'plan',
}

const VERBS: Record<ToolActivityKind, string> = {
  command: 'Ran',
  read: 'Read',
  edit: 'Edited',
  write: 'Wrote',
  search: 'Searched',
  list: 'Listed',
  web: 'Fetched',
  plan: 'Updated plan',
  mcp: 'Called',
  other: '',
}

/** Group headline nouns, e.g. "Ran 2 commands · Read 3 files". */
const GROUP_NOUNS: Record<ToolActivityKind, [string, string]> = {
  command: ['command', 'commands'],
  read: ['file', 'files'],
  edit: ['file', 'files'],
  write: ['file', 'files'],
  search: ['pattern', 'patterns'],
  list: ['path', 'paths'],
  web: ['page', 'pages'],
  plan: ['plan', 'plans'],
  mcp: ['tool', 'tools'],
  other: ['tool', 'tools'],
}

const GROUP_VERBS: Record<ToolActivityKind, string> = {
  ...VERBS,
  web: 'Browsed',
  plan: 'Updated',
  other: 'Used',
}

export function toolActivityKind(name: string): ToolActivityKind {
  const key = name.trim().toLowerCase()
  const known = KIND_BY_NAME[key]
  if (known) return known
  if (key.startsWith('mcp__') || key.includes('/')) return 'mcp'
  return 'other'
}

function parseArgs(argsText: string): Record<string, unknown> | null {
  const text = argsText.trim()
  if (!text.startsWith('{') && !text.startsWith('[')) return null
  try {
    const parsed: unknown = JSON.parse(text)
    if (Array.isArray(parsed)) {
      // Codex can send a bare argv array as the whole argument payload.
      return parsed.length && parsed.every(item => typeof item === 'string') ? { command: parsed } : null
    }
    return parsed && typeof parsed === 'object' ? parsed as Record<string, unknown> : null
  } catch {
    return null
  }
}

function firstString(args: Record<string, unknown> | null, keys: readonly string[]): string {
  if (!args) return ''
  for (const key of keys) {
    const value = args[key]
    if (typeof value === 'string' && value.trim()) return value.trim()
    if (Array.isArray(value) && value.length && value.every(item => typeof item === 'string')) {
      return shellJoin(value as string[])
    }
  }
  return ''
}

/** Codex ``shell`` passes ``["bash", "-lc", "cmd"]``; show only the script. */
function shellJoin(parts: string[]): string {
  if (parts.length === 3 && /(^|\/)(ba|z)?sh$/.test(parts[0]) && /^-l?c$/.test(parts[1])) {
    return parts[2]
  }
  return parts.map(part => (/[\s"'$]/.test(part) ? JSON.stringify(part) : part)).join(' ')
}

function bounded(text: string): string {
  // Slice first: a multi-megabyte argument must not be normalized whole.
  const line = text.slice(0, TARGET_LIMIT * 4).replace(/\s+/g, ' ').trim()
  return line.length > TARGET_LIMIT ? `${line.slice(0, TARGET_LIMIT - 1)}…` : line
}

/** Paths read better relative to wherever the agent works; keep the tail. */
function shortPath(path: string): string {
  const separator = !path.includes('/') && path.includes('\\') ? '\\' : '/'
  const parts = path.split(separator).filter(Boolean)
  return parts.length > 3 ? `…${separator}${parts.slice(-3).join(separator)}` : path
}

/** Bounded line sink: past the limit it records one truncation marker. */
class DiffBuilder {
  readonly lines: ToolDiffLine[] = []
  truncated = false

  push(line: ToolDiffLine): boolean {
    if (this.lines.length >= DIFF_LINE_LIMIT) {
      this.truncated = true
      return false
    }
    this.lines.push(line)
    return true
  }

  result(): ToolDiffLine[] | undefined {
    if (!this.lines.length) return undefined
    return this.truncated ? [...this.lines, { sign: '@', text: '… diff truncated' }] : this.lines
  }
}

function pushLines(out: DiffBuilder, sign: '+' | '-', text: unknown): void {
  if (typeof text !== 'string') return
  for (const line of text.split('\n')) {
    if (!out.push({ sign, text: line })) return
  }
}

const PATCH_FILE_HEADER = /^\*\*\* (Update|Add|Delete) File: (.+)$/

/** Unified diff or Codex patch envelope; file headers become hunk rows. */
function unifiedDiffLines(text: string, out: DiffBuilder): void {
  const lines = text.split('\n')
  if (lines.length > 1 && lines[lines.length - 1] === '') lines.pop()
  for (let index = 0; index < lines.length; index++) {
    const line = lines[index]
    const file = PATCH_FILE_HEADER.exec(line)
    if (file) {
      if (!out.push({ sign: '@', text: file[2].trim() })) return
      continue
    }
    if (line.startsWith('*** ')) continue
    // Git file headers can appear between hunks of a multi-file diff; a
    // ``--- `` line is a header only when a ``+++ `` line follows it, so a
    // removed ``-- comment`` or Markdown rule stays content.
    if (line.startsWith('diff --git ') || line.startsWith('index ')) continue
    if (line.startsWith('--- ') && lines[index + 1]?.startsWith('+++ ')) {
      index++
      continue
    }
    let pushed: boolean
    if (line.startsWith('@@')) pushed = out.push({ sign: '@', text: line })
    else if (line.startsWith('+')) pushed = out.push({ sign: '+', text: line.slice(1) })
    else if (line.startsWith('-')) pushed = out.push({ sign: '-', text: line.slice(1) })
    else pushed = out.push({ sign: ' ', text: line.startsWith(' ') ? line.slice(1) : line })
    if (!pushed) return
  }
}

function changeEntries(changes: unknown): { path: string, diff: unknown }[] {
  if (Array.isArray(changes)) {
    return changes
      .map(change => change as Record<string, unknown> | null)
      .filter((change): change is Record<string, unknown> => Boolean(change) && typeof change?.path === 'string')
      .map(change => ({ path: change.path as string, diff: change.diff ?? change.unified_diff }))
  }
  // Codex also reports changes as a path-keyed map.
  if (changes && typeof changes === 'object') {
    return Object.entries(changes as Record<string, unknown>).map(([path, change]) => {
      const record = change && typeof change === 'object' ? change as Record<string, unknown> : {}
      return { path, diff: record.unified_diff ?? record.diff ?? record.content }
    })
  }
  return []
}

function patchSource(args: Record<string, unknown> | null, argsText: string): string {
  if (typeof args?.input === 'string') return args.input
  if (typeof args?.patch === 'string') return args.patch
  return args ? '' : argsText
}

function editDiff(args: Record<string, unknown> | null, argsText: string): ToolDiffLine[] | undefined {
  const out = new DiffBuilder()
  if (args && ('old_string' in args || 'new_string' in args)) {
    pushLines(out, '-', args.old_string)
    pushLines(out, '+', args.new_string)
  } else if (args && Array.isArray(args.edits)) {
    for (const edit of args.edits as (Record<string, unknown> | null)[]) {
      if (out.lines.length && !out.push({ sign: '@', text: '' })) break
      pushLines(out, '-', edit?.old_string)
      pushLines(out, '+', edit?.new_string)
    }
  } else if (args && args.changes) {
    for (const change of changeEntries(args.changes)) {
      if (!out.push({ sign: '@', text: change.path })) break
      if (typeof change.diff === 'string') unifiedDiffLines(change.diff, out)
    }
  } else {
    const source = patchSource(args, argsText)
    if (source && (source.includes('*** Begin Patch') || source.includes('@@'))) unifiedDiffLines(source, out)
  }
  return out.result()
}

function patchTarget(args: Record<string, unknown> | null, argsText: string): string {
  const paths = changeEntries(args?.changes).map(change => change.path)
  if (paths.length === 1) return shortPath(paths[0])
  if (paths.length > 1) return `${paths.length} files`
  const source = patchSource(args, argsText)
  const files = [...source.matchAll(/^\*\*\* (?:Update|Add|Delete) File: (.+)$/gm)].map(match => match[1].trim())
  if (files.length === 1) return shortPath(files[0])
  if (files.length > 1) return `${files.length} files`
  return ''
}

function mcpLabel(name: string): string {
  const parts = name.split('__').filter(Boolean)
  return parts.length >= 3 ? `${parts[1]} · ${parts.slice(2).join('__')}` : name
}

type ToolLike = Pick<TimelineTool, 'name' | 'argsText'>

// Tool rows are mutated in place while streaming, so the cache is keyed by the
// row and validated against the inputs it was derived from.
const activityCache = new WeakMap<ToolLike, { name: string, argsText: string, activity: ToolActivity }>()

/** Describe one call for its activity line, parsing ``argsText`` at most once
 *  per distinct value. */
export function describeToolActivity(tool: ToolLike): ToolActivity {
  const cached = activityCache.get(tool)
  if (cached && cached.name === tool.name && cached.argsText === tool.argsText) return cached.activity
  // A multi-megabyte argument streaming in would otherwise be re-parsed on
  // every delta. While it is still incomplete JSON, refresh only per chunk.
  if (
    cached && cached.name === tool.name &&
    tool.argsText.length > LARGE_ARGS && tool.argsText.length - cached.argsText.length < LARGE_ARGS_STEP &&
    !/[}\]]\s*$/.test(tool.argsText.slice(-64))
  ) return cached.activity
  const activity = deriveToolActivity(tool)
  activityCache.set(tool, { name: tool.name, argsText: tool.argsText, activity })
  return activity
}

function deriveToolActivity(tool: ToolLike): ToolActivity {
  const kind = toolActivityKind(tool.name)
  const args = parseArgs(tool.argsText)
  const verb = VERBS[kind]
  switch (kind) {
    case 'command': {
      // Follow-ups on an existing shell session carry no command of their own.
      const lower = tool.name.toLowerCase()
      if (lower === 'write_stdin') return { kind, verb: 'Sent input', target: bounded(firstString(args, ['chars', 'input', 'text'])) }
      if (lower === 'bashoutput') return { kind, verb: 'Read output', target: bounded(firstString(args, ['bash_id', 'shell_id', 'session_id'])) }
      const command = firstString(args, ['command', 'cmd', 'script', 'commandLine']) ||
        (args ? '' : tool.argsText.trim())
      return { kind, verb, target: bounded(command), ...(command ? { command } : {}) }
    }
    case 'read':
    case 'write':
      return {
        kind,
        verb: tool.name.toLowerCase().startsWith('delete') ? 'Deleted' : verb,
        target: shortPath(firstString(args, ['file_path', 'path', 'target_file', 'notebook_path', 'filePath'])),
      }
    case 'edit': {
      const path = firstString(args, ['file_path', 'path', 'target_file', 'notebook_path', 'filePath'])
      return {
        kind,
        verb,
        target: path ? shortPath(path) : patchTarget(args, tool.argsText),
        diff: editDiff(args, tool.argsText),
      }
    }
    case 'search': {
      const pattern = firstString(args, ['pattern', 'query', 'regex', 'search_term'])
      const where = firstString(args, ['path', 'glob', 'include', 'target_directories'])
      const target = pattern && where ? `${pattern} in ${shortPath(where)}` : pattern || shortPath(where)
      return { kind, verb, target: bounded(target) }
    }
    case 'list':
      return { kind, verb, target: bounded(firstString(args, ['pattern', 'path', 'target_directory', 'directory'])) }
    case 'web': {
      const url = firstString(args, ['url'])
      const query = firstString(args, ['query', 'q'])
      return { kind, verb: url ? 'Fetched' : 'Searched web', target: bounded(url || query) }
    }
    case 'plan':
      return { kind, verb, target: '' }
    case 'mcp':
      return { kind, verb, target: mcpLabel(tool.name) }
    default:
      return { kind, verb: tool.name, target: '' }
  }
}

/**
 * One headline for a group of calls. Classifies by name only, so a long
 * running turn never re-parses every argument blob on each render.
 */
export function toolGroupHeadline(tools: readonly Pick<TimelineTool, 'name'>[]): string {
  const counts = new Map<ToolActivityKind, number>()
  for (const tool of tools) {
    const kind = toolActivityKind(tool.name)
    counts.set(kind, (counts.get(kind) ?? 0) + 1)
  }
  const phrases: string[] = []
  for (const [kind, count] of counts) {
    const [one, many] = GROUP_NOUNS[kind]
    const verb = phrases.length ? GROUP_VERBS[kind].toLowerCase() : GROUP_VERBS[kind]
    phrases.push(`${verb} ${count} ${count === 1 ? one : many}`)
  }
  const shown = phrases.slice(0, 3)
  const text = shown.join(', ')
  return phrases.length > shown.length ? `${text}, …` : text
}

export interface ToolGroupSummary {
  kind: ToolActivityKind
  verb: string
  target: string
}

const groupCache = new WeakMap<readonly ToolLike[], { length: number, summary: ToolGroupSummary }>()

/** Headline for a whole group: a single call names its action, several calls
 *  name what they did in aggregate. Groups only grow by appending, so the
 *  aggregate is cached per array and recomputed when its length changes. */
export function toolGroupSummary(tools: readonly ToolLike[]): ToolGroupSummary {
  if (tools.length === 1) {
    const { kind, verb, target } = describeToolActivity(tools[0])
    return { kind, verb, target }
  }
  const cached = groupCache.get(tools)
  if (cached && cached.length === tools.length) return cached.summary
  const kinds = new Set(tools.map(tool => toolActivityKind(tool.name)))
  const summary: ToolGroupSummary = {
    kind: kinds.size === 1 ? [...kinds][0] : 'other',
    verb: toolGroupHeadline(tools),
    target: '',
  }
  groupCache.set(tools, { length: tools.length, summary })
  return summary
}
