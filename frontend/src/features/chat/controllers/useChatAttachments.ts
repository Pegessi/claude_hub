import { type Ref, computed, ref } from 'vue'
import { type StreamCapabilities } from '@/types'
import { type DraftAttachment } from './types'
import { validateImageAttachment, fileToDataUrl, generatePreviewDataUrl } from '@/composables/useAgentStream'
import { attachmentRequestBytes } from '@/utils/agentStreamAttachments'

interface Dependencies {
  capabilities: Ref<StreamCapabilities | null>
  composerError: Ref<string | null>
  goalComposerReason: Ref<string | null>
  goalInputBlocked: Ref<boolean>
  isGoalMutating: Ref<boolean>
  isSending: Ref<boolean>
}

export function useChatAttachments(deps: Dependencies) {
  const attachments = ref<DraftAttachment[]>([])

  const isPreparingAttachments = ref(false)

  /** Monotonically increasing epoch that advances on every source switch
   *  (session/tab change) and on unmount. Captured at the start of an
   *  attachment preparation batch and re-checked after every await; an exact
   *  match is required so a stale batch from a previous source visit cannot
   *  append into a later visit of the same source (the ABA problem: switch
   *  A→B→A while FileReader/canvas awaits, and the stale A batch would
   *  otherwise pass a source-string equality check). */
  const preparationEpoch = ref(0)

  const isDragOver = ref(false)

  const fileInputEl = ref<HTMLInputElement | null>(null)

  const supportsImages = computed(() => deps.capabilities.value?.supports_images ?? false)

  const attachmentDisabledReason = computed(() => {
    if (!supportsImages.value) return 'This chat does not support image attachments'
    if (deps.goalInputBlocked.value) return deps.goalComposerReason.value
    if (deps.isSending.value || isPreparingAttachments.value || deps.isGoalMutating.value) return 'Wait for the current upload or send to finish'
    return null
  })

  function triggerFilePicker() {
    if (attachmentDisabledReason.value) return
    fileInputEl.value?.click()
  }

  const MAX_ATTACHMENTS = 10

  // Backend enforces a 40 MiB total decoded cap per send request (originals plus
  // re-encoded previews). The backend remains authoritative.
  const MAX_TOTAL_REQUEST_BYTES = 40 * 1024 * 1024

  const TOTAL_REQUEST_LIMIT_ERROR =
    `Total attachment size exceeds the ${MAX_TOTAL_REQUEST_BYTES / 1024 / 1024} MiB request limit.`

  async function addFiles(files: FileList | File[]) {
    // Capture the epoch BEFORE any state mutation. If the source switches
    // (or the component unmounts) during this batch, the epoch advances and
    // every post-await check fails, so this stale batch never touches the
    // new source's composer state.
    const epoch = preparationEpoch.value

    deps.composerError.value = null
    if (!supportsImages.value) {
      deps.composerError.value = 'This chat does not support image attachments.'
      return
    }
    if (deps.goalInputBlocked.value) {
      deps.composerError.value = deps.goalComposerReason.value
      return
    }
    // Serialize preparation batches: reject new input while a previous batch is
    // still reading/generating previews. Without this, two concurrent batches
    // both set isPreparingAttachments=true; the first to finish clears it and
    // re-enables Send while the second is still mid-await.
    if (isPreparingAttachments.value) return
    // Block new attachments while a send is in flight: an async continuation
    // must never append into a composer that has already been cleared and sent.
    if (deps.isSending.value) return

    const list = Array.from(files)
    // Enforce the max-attachment count client-side.
    const remaining = MAX_ATTACHMENTS - attachments.value.length
    if (remaining <= 0) {
      deps.composerError.value = `You can attach up to ${MAX_ATTACHMENTS} images.`
      return
    }
    const accepted = list.slice(0, remaining)
    if (accepted.length < list.length) {
      deps.composerError.value = `You can attach up to ${MAX_ATTACHMENTS} images.`
    }

    // Enforce the total request byte cap before reading: existing draft bytes
    // plus the new originals must fit. A re-encoded preview is counted once it
    // exists (below).
    const existingBytes = attachments.value.reduce((sum, a) => sum + attachmentRequestBytes(a), 0)
    const newBytes = accepted.reduce((sum, f) => sum + f.size, 0)
    if (existingBytes + newBytes > MAX_TOTAL_REQUEST_BYTES) {
      deps.composerError.value = TOTAL_REQUEST_LIMIT_ERROR
      return
    }

    // Validate all accepted files before starting any async work.
    for (const file of accepted) {
      const err = validateImageAttachment(file)
      if (err) {
        deps.composerError.value = err
        return
      }
    }

    // Mark the entire preparation batch active BEFORE the first await so Send
    // stays disabled for the whole read+preview-generation window.
    isPreparingAttachments.value = true

    try {
      for (const file of accepted) {
        // Read the original once. The provisional thumbnail uses the original
        // data URL so it appears instantly; the bounded preview is generated
        // from the same data URL (no second read).
        let dataUrl: string
        try {
          dataUrl = await fileToDataUrl(file)
        } catch (e) {
          // Only surface the error if this batch still owns the composer.
          if (preparationEpoch.value === epoch) {
            deps.composerError.value = e instanceof Error
              ? `Failed to read ${file.name}: ${e.message}`
              : `Failed to read ${file.name}.`
          }
          continue
        }
        // Stale batch (source switched or send started): bail out WITHOUT
        // clearing isPreparingAttachments — the new source's batch may own it.
        if (preparationEpoch.value !== epoch || deps.isSending.value) return

        const provisionalId = `att-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
        const provisional: DraftAttachment = {
          id: provisionalId,
          filename: file.name,
          mime_type: file.type,
          data_url: dataUrl,
          // Provisional thumbnail uses the original data URL.
          preview_url: dataUrl,
          size_bytes: file.size,
        }
        attachments.value.push(provisional)

        try {
          // Pass the already-read data URL so we do not read the file twice.
          const previewDataUrl = await generatePreviewDataUrl(file, dataUrl)
          // Stale batch: bail out without clearing the flag.
          if (preparationEpoch.value !== epoch || deps.isSending.value) return
          const idx = attachments.value.findIndex((a) => a.id === provisionalId)
          if (idx < 0) continue // removed by the user while preparing
          const prepared: DraftAttachment = {
            ...provisional,
            preview_data_url: previewDataUrl,
            // Replace the provisional thumbnail with the bounded preview.
            preview_url: previewDataUrl,
          }
          const total = attachments.value.reduce(
            (sum, a) => sum + attachmentRequestBytes(a.id === provisionalId ? prepared : a),
            0,
          )
          if (total > MAX_TOTAL_REQUEST_BYTES) {
            attachments.value = attachments.value.filter((a) => a.id !== provisionalId)
            deps.composerError.value = TOTAL_REQUEST_LIMIT_ERROR
            continue
          }
          attachments.value[idx] = prepared
        } catch (e) {
          // Preview generation failed. Only mutate state if this batch still
          // owns the composer; otherwise leave the new source's state alone.
          if (preparationEpoch.value !== epoch) return
          attachments.value = attachments.value.filter((a) => a.id !== provisionalId)
          deps.composerError.value = e instanceof Error
            ? `Failed to prepare ${file.name}: ${e.message}`
            : `Failed to prepare ${file.name}.`
        }
      }
    } finally {
      // Only the owning batch may clear the preparing flag. A stale batch
      // (epoch advanced) must not unlock a newer batch that set the flag.
      if (preparationEpoch.value === epoch) {
        isPreparingAttachments.value = false
      }
    }
  }

  function handleFilePick(event: Event) {
    const input = event.target as HTMLInputElement
    if (input.files && input.files.length > 0) {
      void addFiles(input.files)
    }
    input.value = ''
  }

  function handlePaste(event: ClipboardEvent) {
    if (deps.isSending.value) return
    const items = event.clipboardData?.items
    if (!items) return
    const files: File[] = []
    for (const item of items) {
      if (item.kind === 'file') {
        const f = item.getAsFile()
        if (f) files.push(f)
      }
    }
    if (files.length > 0) {
      event.preventDefault()
      void addFiles(files)
    }
  }

  function handleDragOver(event: DragEvent) {
    event.preventDefault()
    if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy'
    isDragOver.value = true
  }

  function handleDragLeave() {
    isDragOver.value = false
  }

  function handleDrop(event: DragEvent) {
    event.preventDefault()
    isDragOver.value = false
    if (deps.isSending.value) return
    const files = event.dataTransfer?.files
    if (files && files.length > 0) {
      void addFiles(files)
    }
  }

  function removeAttachment(att: DraftAttachment) {
    if (deps.isSending.value) return
    const idx = attachments.value.findIndex((a) => a.id === att.id)
    if (idx >= 0) attachments.value.splice(idx, 1)
  }

  return {
    attachments,
    isPreparingAttachments,
    preparationEpoch,
    isDragOver,
    fileInputEl,
    supportsImages,
    attachmentDisabledReason,
    triggerFilePicker,
    MAX_ATTACHMENTS,
    MAX_TOTAL_REQUEST_BYTES,
    TOTAL_REQUEST_LIMIT_ERROR,
    addFiles,
    handleFilePick,
    handlePaste,
    handleDragOver,
    handleDragLeave,
    handleDrop,
    removeAttachment,
  }
}
