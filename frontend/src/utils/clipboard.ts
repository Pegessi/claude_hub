/**
 * Copy text to the clipboard, with a fallback for non-secure contexts.
 *
 * Prefers the async Clipboard API (available on HTTPS / localhost). Falls back
 * to a hidden textarea + execCommand for older browsers or HTTP origins where
 * navigator.clipboard is undefined. Throws if neither path succeeds.
 */
export async function writeClipboard(value: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(value)
    return
  }

  const textarea = document.createElement('textarea')
  textarea.value = value
  textarea.setAttribute('readonly', '')
  textarea.style.position = 'fixed'
  textarea.style.top = '-999px'
  document.body.appendChild(textarea)
  textarea.select()
  const didCopy = document.execCommand('copy')
  document.body.removeChild(textarea)
  if (!didCopy) {
    throw new Error('copy failed')
  }
}
