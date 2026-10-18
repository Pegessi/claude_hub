export type TerminalThemePayload = {
  scheme: string
  minimumContrastRatio: number
  page: {
    background: string
    canvasFilter: string
    foreground: string
    selection: string
  }
  xterm: Record<string, string>
}

function cssVar(name: string) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim()
}

function terminalColor(name: string, fallbackName?: string) {
  return cssVar(name) || (fallbackName ? cssVar(fallbackName) : '')
}

export function createTerminalThemePayload(scheme: string): TerminalThemePayload {
  const background = cssVar('--ch-terminal-bg')
  const foreground = cssVar('--ch-terminal-fg')
  return {
    scheme,
    minimumContrastRatio: scheme === 'light' ? 4.5 : 3,
    page: {
      background,
      canvasFilter: cssVar('--ch-terminal-canvas-filter') || 'none',
      foreground,
      selection: cssVar('--ch-terminal-selection'),
    },
    xterm: {
      background,
      foreground,
      cursor: cssVar('--ch-terminal-cursor'),
      cursorAccent: cssVar('--ch-terminal-bg'),
      cursorInactiveColor: cssVar('--ch-terminal-cursor'),
      selectionBackground: cssVar('--ch-terminal-selection'),
      black: cssVar('--ch-terminal-black'),
      red: cssVar('--ch-terminal-red'),
      green: cssVar('--ch-terminal-green'),
      yellow: cssVar('--ch-terminal-yellow'),
      blue: cssVar('--ch-terminal-blue'),
      magenta: cssVar('--ch-terminal-magenta'),
      cyan: cssVar('--ch-terminal-cyan'),
      white: cssVar('--ch-terminal-white'),
      brightBlack: cssVar('--ch-terminal-bright-black'),
      brightRed: terminalColor('--ch-terminal-bright-red', '--ch-terminal-red'),
      brightGreen: terminalColor('--ch-terminal-bright-green', '--ch-terminal-green'),
      brightYellow: terminalColor('--ch-terminal-bright-yellow', '--ch-terminal-yellow'),
      brightBlue: terminalColor('--ch-terminal-bright-blue', '--ch-terminal-blue'),
      brightMagenta: terminalColor('--ch-terminal-bright-magenta', '--ch-terminal-magenta'),
      brightCyan: terminalColor('--ch-terminal-bright-cyan', '--ch-terminal-cyan'),
      brightWhite: cssVar('--ch-terminal-bright-white'),
    },
  }
}

export function terminalThemePayloadKey(payload: TerminalThemePayload): string {
  try {
    return JSON.stringify(payload)
  } catch {
    return String(Math.random())
  }
}
