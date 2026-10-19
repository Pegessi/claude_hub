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
const DIFF_LINE_LIMIT = 400

const KIND_BY_NAME: Record<string, ToolActivityKind> = {
  bash: 'command',
  shell: 'command',
  exec_command: 'command',
  local_shell: 'command',
  run_terminal_cmd: 'command',
  terminal: 'command',
  command: 'command',
  read: 'read',
  read_file: 'read',
  view: 'read',
  notebookread: 'read',
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
  search: ['search', 'searches'],
  list: ['listing', 'listings'],
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
  if (!text.startsWith('{')) return null
  try {
    const parsed: unknown = JSON.parse(text)
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      ? parsed as Record<string, unknown>
      : null
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
  const line = text.replace(/\s+/g, ' ').trim()
  return line.length > TARGET_LIMIT ? `${line.slice(0, TARGET_LIMIT - 1)}…` : line
}

/** Paths read better relative to wherever the agent works; keep the tail. */
function shortPath(path: string): string {
  const parts = path.split('/').filter(Boolean)
  return parts.length > 3 ? `…/${parts.slice(-3).join('/')}` : path
}

function pushLines(out: ToolDiffLine[], sign: '+' | '-', text: unknown): void {
  if (typeof text !== 'string') return
  for (const line of text.split('\n')) {
    if (out.length >= DIFF_LINE_LIMIT) return
    out.push({ sign, text: line })
  }
}

function unifiedDiffLines(text: string, out: ToolDiffLine[]): void {
  for (const line of text.split('\n')) {
    if (out.length >= DIFF_LINE_LIMIT) return
    if (line.startsWith('+++') || line.startsWith('---') || line.startsWith('*** ')) continue
    if (line.startsWith('@@')) out.push({ sign: '@', text: line })
    else if (line.startsWith('+')) out.push({ sign: '+', text: line.slice(1) })
    else if (line.startsWith('-')) out.push({ sign: '-', text: line.slice(1) })
    else out.push({ sign: ' ', text: line.startsWith(' ') ? line.slice(1) : line })
  }
}

function editDiff(args: Record<string, unknown> | null, argsText: string): ToolDiffLine[] | undefined {
  const out: ToolDiffLine[] = []
  if (args) {
    if ('old_string' in args || 'new_string' in args) {
      pushLines(out, '-', args.old_string)
      pushLines(out, '+', args.new_string)
    } else if (Array.isArray(args.edits)) {
      for (const edit of args.edits as Record<string, unknown>[]) {
        if (out.length) out.push({ sign: '@', text: '' })
        pushLines(out, '-', edit?.old_string)
        pushLines(out, '+', edit?.new_string)
      }
    } else if (Array.isArray(args.changes)) {
      for (const change of args.changes as Record<string, unknown>[]) {
        if (typeof change?.path === 'string') out.push({ sign: '@', text: change.path })
        if (typeof change?.diff === 'string') unifiedDiffLines(change.diff, out)
      }
    } else if (typeof args.input === 'string' || typeof args.patch === 'string') {
      unifiedDiffLines(String(args.input ?? args.patch), out)
    }
  } else if (argsText.includes('*** Begin Patch')) {
    unifiedDiffLines(argsText, out)
  }
  return out.length ? out : undefined
}

function patchTarget(args: Record<string, unknown> | null, argsText: string): string {
  const changes = args?.changes
  if (Array.isArray(changes)) {
    const paths = changes
      .map(change => (change as Record<string, unknown>)?.path)
      .filter((path): path is string => typeof path === 'string')
    if (paths.length === 1) return shortPath(paths[0])
    if (paths.length > 1) return `${paths.length} files`
  }
  const source = typeof args?.input === 'string' ? args.input : argsText
  const files = [...source.matchAll(/\*\*\* (?:Update|Add|Delete) File: (.+)/g)].map(match => match[1].trim())
  if (files.length === 1) return shortPath(files[0])
  if (files.length > 1) return `${files.length} files`
  return ''
}

function mcpLabel(name: string): string {
  const parts = name.split('__').filter(Boolean)
  return parts.length >= 3 ? `${parts[1]} · ${parts.slice(2).join('__')}` : name
}

/** Describe one call for its activity line. Parses ``argsText`` once. */
export function describeToolActivity(tool: Pick<TimelineTool, 'name' | 'argsText'>): ToolActivity {
  const kind = toolActivityKind(tool.name)
  const args = parseArgs(tool.argsText)
  const verb = VERBS[kind]
  switch (kind) {
    case 'command': {
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
      return { kind, verb, target: bounded(where ? `${pattern} in ${shortPath(where)}` : pattern) }
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
