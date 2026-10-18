const terminalRendererQuery = (() => {
  try {
    if (
      typeof window !== 'undefined' &&
      typeof window.matchMedia === 'function' &&
      window.matchMedia('(pointer: coarse)').matches
    ) {
      return '?rendererType=webgl'
    }
  } catch {
    // Browsers without matchMedia use the ttyd server's canvas default.
  }
  return ''
})()

export function terminalIframeSrc(tabId: string): string {
  return `/api/terminal/proxy/${tabId}/${terminalRendererQuery}`
}
