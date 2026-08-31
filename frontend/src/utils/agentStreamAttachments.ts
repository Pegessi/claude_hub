/**
 * Pure helpers for the structured composer's image attachment pipeline.
 *
 * Kept separate from ``useAgentStream`` so they can be unit-tested without
 * pulling in Vue's reactivity runtime.
 */

const SUPPORTED_IMAGE_MIME = /^image\/(png|jpeg|gif|webp|bmp)$/i
const MAX_IMAGE_BYTES = 8 * 1024 * 1024 // 8 MB

/**
 * Validate an image attachment for the structured composer.
 *
 * Returns null on success, or a human-readable error string on failure.
 * The caller is responsible for surfacing the error in the UI.
 */
export function validateImageAttachment(file: File): string | null {
  if (!SUPPORTED_IMAGE_MIME.test(file.type)) {
    return `Unsupported image type: ${file.type || 'unknown'}. Use PNG, JPEG, GIF, WebP, or BMP.`
  }
  if (file.size > MAX_IMAGE_BYTES) {
    return `Image ${file.name} is ${(file.size / 1024 / 1024).toFixed(1)} MB; the limit is 8 MB.`
  }
  return null
}

/**
 * Read a File into a data URL for the ``WorkspaceAttachmentCreate`` contract.
 */
export function fileToDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(reader.result as string)
    reader.onerror = () => reject(reader.error)
    reader.readAsDataURL(file)
  })
}
