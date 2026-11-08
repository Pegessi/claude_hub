import { type Ref, ref } from 'vue'
import type { AttachmentTarget } from '@/composables/useAttachmentPreview'
import type { TimelineAttachment, TimelinePart, TimelineTool } from '../timeline/types'

interface Dependencies {
  streamBasePath: Ref<string>
  openPreview: (target: AttachmentTarget, event?: MouseEvent) => void
}

export function useChatImages(deps: Dependencies) {
  /** Attachment ids whose preview fetch returned 404/410 (evicted or never
   * cached). Rendered as a visible "Preview expired" placeholder. */
  const erroredAttachments = ref<Set<string>>(new Set())

  /** Agent-image part keys whose scoped image fetch failed (the agent deleted
   * or moved the file, or it is no longer an allowed image). Those blocks
   * degrade to a stable path-only placeholder instead of a broken icon.
   * The set is not a permanent tombstone: an explicit user retry removes the
   * key so the same authorized URL is requested again. */
  const erroredAgentImages = ref<Set<string>>(new Set())

  /** Per-part retry counter. A positive value adds a cache-busting query so
   * the retried request is fresh while the authorized scoped URL is reused. */
  const agentImageRetryNonces = ref<Map<string, number>>(new Map())

  function openImageLightbox(url: string, alt: string, event: MouseEvent) {
    deps.openPreview({ scope: deps.streamBasePath.value, path: '', imageUrl: url, name: alt }, event)
  }

  /**
   * Build the scoped preview URL for an authoritative attachment id.
   *
   * The endpoint is session/tab-scoped so a leaked id from another session
   * cannot be used to fetch previews. The backend validates the id against the
   * session/tab manifest before serving.
   */
  function attachmentUrl(attachmentId: string): string {
    const encId = encodeURIComponent(attachmentId)
    return `${deps.streamBasePath.value}/attachments/${encId}`
  }

  /**
   * Build the scoped URL for an agent-produced local image (view_image / image
   * Read). The backend resolves ``path`` only inside the tab's working dir and
   * validates magic bytes before serving, so this is not an arbitrary file read.
   * A retry nonce only forces a fresh request; it never changes authorization.
   */
  function agentImageUrl(path: string, partKey?: string): string {
    const base = `${deps.streamBasePath.value}/agent-image?path=${encodeURIComponent(path)}`
    if (!partKey) return base
    const nonce = agentImageRetryNonces.value.get(partKey) ?? 0
    return nonce > 0 ? `${base}&retry=${nonce}` : base
  }

  /** Short file name for the image block's subtitle (full path stays on hover). */
  function agentImageBasename(path: string): string {
    const clean = path.split(/[?#]/, 1)[0].replace(/\\/g, '/')
    const parts = clean.split('/')
    return parts[parts.length - 1] || clean
  }

  /**
   * Out-of-cwd image grants exist only after the producing tool completes
   * successfully, so a still-running image tool must not issue a request that
   * would 404 and create a permanent placeholder. A non-running status
   * (completed/failed/cancelled — including an interrupted turn whose tool
   * never received a completion) is treated as settled and rendered normally,
   * so the UI can never get stuck in a permanent "loading" state.
   */
  function isAgentImagePending(
    part: Extract<TimelinePart, { kind: 'agent_image' }>,
    active = true,
  ): boolean {
    // A running tool only blocks the image while its turn/thread is still
    // active. Once the surrounding turn is completed/interrupted (active =
    // false) the part is settled: it renders the image or, on fetch failure,
    // the placeholder with manual retry — never a permanent "loading".
    if (!active) return false
    const status = (part.tool as TimelineTool | undefined)?.status
    return status === 'running'
  }

  function onAgentImageError(_event: Event, partKey: string): void {
    erroredAgentImages.value = new Set(erroredAgentImages.value).add(partKey)
  }

  /**
   * Explicit user-controlled retry: clear the failed marker and bump the nonce
   * so the exact part re-requests its existing authorized URL. There is no
   * automatic retry loop and no tool replay; if the reload fails again the
   * placeholder returns and the control stays available for another manual try.
   */
  function retryAgentImage(partKey: string): void {
    const next = new Map(agentImageRetryNonces.value)
    next.set(partKey, (next.get(partKey) ?? 0) + 1)
    agentImageRetryNonces.value = next
    const remaining = new Set(erroredAgentImages.value)
    remaining.delete(partKey)
    erroredAgentImages.value = remaining
  }

  /**
   * Record an attachment whose preview fetch failed (404/410 or network error).
   *
   * The template renders a visible "Preview expired" placeholder for ids in
   * ``erroredAttachments`` instead of a broken image icon. The set is keyed by
   * attachment id; a re-render of the turn (e.g. on reconnect) does not clear
   * it, so the placeholder stays stable.
   */
  function onAttachmentError(_event: Event, att: TimelineAttachment): void {
    if (att.id === null) return
    erroredAttachments.value = new Set(erroredAttachments.value).add(att.id)
  }

  return {
    erroredAttachments,
    erroredAgentImages,
    agentImageRetryNonces,
    openImageLightbox,
    attachmentUrl,
    agentImageUrl,
    agentImageBasename,
    isAgentImagePending,
    onAgentImageError,
    retryAgentImage,
    onAttachmentError,
  }
}
