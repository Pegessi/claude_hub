import { type Ref, nextTick, ref } from 'vue'
import { type TimelineAttachment } from '../timeline/types'

interface Dependencies {
  streamBasePath: Ref<string>
}

export function useChatImages(deps: Dependencies) {
  /** Attachment ids whose preview fetch returned 404/410 (evicted or never
   *  cached). Rendered as a visible "Preview expired" placeholder. */
  const erroredAttachments = ref<Set<string>>(new Set())

  /** Agent-image part keys whose scoped image fetch failed (the agent deleted
   *  or moved the file, or it is no longer an allowed image). Those blocks
   *  degrade to a stable path-only placeholder instead of a broken icon. */
  const erroredAgentImages = ref<Set<string>>(new Set())

  const imageLightboxUrl = ref<string | null>(null)

  const imageLightboxAlt = ref('')

  const imageLightboxActualSize = ref(false)

  const imageLightboxDimensions = ref('')

  const lightboxEl = ref<HTMLElement | null>(null)

  const lightboxCloseEl = ref<HTMLButtonElement | null>(null)

  let imageLightboxTrigger: HTMLElement | null = null

  function openImageLightbox(url: string, alt: string, event: MouseEvent) {
    imageLightboxUrl.value = url
    imageLightboxAlt.value = alt
    imageLightboxActualSize.value = false
    imageLightboxDimensions.value = ''
    const delegated = event.target instanceof Element
      ? event.target.closest<HTMLElement>('button[data-image-preview]') : null
    imageLightboxTrigger = delegated || (event.currentTarget instanceof HTMLElement
      ? event.currentTarget
      : null)
    void nextTick(() => lightboxCloseEl.value?.focus())
  }

  function onLightboxImageLoad(event: Event) {
    const image = event.target as HTMLImageElement
    imageLightboxDimensions.value = `${image.naturalWidth} × ${image.naturalHeight}`
  }

  function dismissImageLightbox(restoreFocus: boolean) {
    if (!imageLightboxUrl.value) return
    const trigger = imageLightboxTrigger
    imageLightboxUrl.value = null
    imageLightboxAlt.value = ''
    imageLightboxTrigger = null
    if (restoreFocus) void nextTick(() => trigger?.focus())
  }

  function closeImageLightbox() {
    dismissImageLightbox(true)
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
   */
  function agentImageUrl(path: string): string {
    return `${deps.streamBasePath.value}/agent-image?path=${encodeURIComponent(path)}`
  }

  /** Short file name for the image block's subtitle (full path stays on hover). */
  function agentImageBasename(path: string): string {
    const clean = path.split(/[?#]/, 1)[0].replace(/\\/g, '/')
    const parts = clean.split('/')
    return parts[parts.length - 1] || clean
  }

  function onAgentImageError(_event: Event, partKey: string): void {
    erroredAgentImages.value = new Set(erroredAgentImages.value).add(partKey)
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
    imageLightboxUrl,
    imageLightboxAlt,
    imageLightboxActualSize,
    imageLightboxDimensions,
    lightboxEl,
    lightboxCloseEl,
    openImageLightbox,
    onLightboxImageLoad,
    dismissImageLightbox,
    closeImageLightbox,
    attachmentUrl,
    agentImageUrl,
    agentImageBasename,
    onAgentImageError,
    onAttachmentError,
  }
}
