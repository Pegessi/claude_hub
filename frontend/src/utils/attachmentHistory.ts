/** Explicitly distinguish preview history from real Chat navigation. */
const previewNavigations = new WeakSet<PopStateEvent>()

export function markAttachmentNavigation(event: PopStateEvent, previewUrl: string): void {
  // A multi-entry Back can leave the preview AND navigate to a different Chat.
  if (window.location.href === previewUrl) previewNavigations.add(event)
}

export function isAttachmentNavigation(event: PopStateEvent): boolean {
  return previewNavigations.has(event) || (
    typeof event.state?.attachmentPreview === 'string'
    && event.state.attachmentPreviewUrl === window.location.href
  )
}

/** Consume a preview entry even when its component is being unmounted. */
export function backFromAttachmentPreview(marker: string, previewUrl: string): boolean {
  if (window.history.state?.attachmentPreview !== marker || window.location.href !== previewUrl) return false
  // Register before back(): component cleanup may remove its own listener before
  // this asynchronous traversal reaches App's routing listener.
  window.addEventListener('popstate', event => markAttachmentNavigation(event, previewUrl), {
    capture: true, once: true,
  })
  window.history.back()
  return true
}
