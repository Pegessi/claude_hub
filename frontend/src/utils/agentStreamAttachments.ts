/**
 * Pure helpers for the structured composer's image attachment pipeline.
 *
 * Kept separate from ``useAgentStream`` so they can be unit-tested without
 * pulling in Vue's reactivity runtime.
 */

const SUPPORTED_IMAGE_MIME = /^image\/(png|jpeg|gif|webp)$/i
const MAX_IMAGE_BYTES = 8 * 1024 * 1024 // 8 MB

/** Bounded preview constraints enforced by the browser before upload. */
export const MAX_PREVIEW_EDGE = 1024
export const MAX_PREVIEW_BYTES = 512 * 1024 // 512 KiB

/**
 * Validate an image attachment for the structured composer.
 *
 * Returns null on success, or a human-readable error string on failure.
 * The caller is responsible for surfacing the error in the UI.
 */
export function validateImageAttachment(file: File): string | null {
  if (!SUPPORTED_IMAGE_MIME.test(file.type)) {
    return `Unsupported image type: ${file.type || 'unknown'}. Use PNG, JPEG, GIF, or WebP.`
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

function loadImage(src: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const img = new Image()
    img.onload = () => resolve(img)
    img.onerror = () => reject(new Error('Failed to decode image for preview'))
    img.src = src
  })
}

function canvasToBlob(
  canvas: HTMLCanvasElement,
  type: string,
  quality?: number,
): Promise<Blob | null> {
  return new Promise((resolve) => {
    canvas.toBlob((blob) => resolve(blob), type, quality)
  })
}

function blobToDataUrl(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(reader.result as string)
    reader.onerror = () => reject(reader.error)
    reader.readAsDataURL(blob)
  })
}

/**
 * Generate a bounded preview data URL for an image attachment.
 *
 * The preview is downscaled so its longest edge does not exceed
 * ``MAX_PREVIEW_EDGE`` (1024px) and encoded as JPEG. If the resulting blob
 * still exceeds ``MAX_PREVIEW_BYTES`` (512 KiB), the JPEG quality is reduced
 * in steps until it fits. The preview MIME may differ from the original
 * (e.g. a PNG original is transcoded to JPEG) — the backend accepts this.
 *
 * The original ``data_url`` is sent to the provider; only this bounded
 * ``preview_data_url`` is persisted to the attachment cache.
 *
 * @param file The original image file.
 * @param dataUrl Optional pre-read data URL of the original. When supplied,
 *   the file is not read again (single-read preparation).
 */
export async function generatePreviewDataUrl(file: File, dataUrl?: string): Promise<string> {
  const originalDataUrl = dataUrl ?? await fileToDataUrl(file)
  const img = await loadImage(originalDataUrl)

  // Scale so the longest edge <= MAX_PREVIEW_EDGE.
  const longestEdge = Math.max(img.naturalWidth, img.naturalHeight)
  const scale = longestEdge > MAX_PREVIEW_EDGE ? MAX_PREVIEW_EDGE / longestEdge : 1
  const width = Math.round(img.naturalWidth * scale)
  const height = Math.round(img.naturalHeight * scale)

  const canvas = document.createElement('canvas')
  canvas.width = width
  canvas.height = height
  const ctx = canvas.getContext('2d')
  if (!ctx) {
    throw new Error('Canvas 2D context unavailable for preview generation')
  }
  ctx.drawImage(img, 0, 0, width, height)

  // Encode as JPEG; reduce quality until the blob fits under MAX_PREVIEW_BYTES.
  let quality = 0.85
  let blob = await canvasToBlob(canvas, 'image/jpeg', quality)
  while (blob && blob.size > MAX_PREVIEW_BYTES && quality > 0.1) {
    quality -= 0.1
    blob = await canvasToBlob(canvas, 'image/jpeg', quality)
  }

  if (!blob) {
    throw new Error('Failed to encode preview image')
  }
  if (blob.size > MAX_PREVIEW_BYTES) {
    // Last-resort: scale the canvas down by half and retry. This should be
    // extremely rare for a 1024px JPEG.
    const half = document.createElement('canvas')
    half.width = Math.max(1, Math.round(width / 2))
    half.height = Math.max(1, Math.round(height / 2))
    const hctx = half.getContext('2d')
    if (!hctx) throw new Error('Canvas 2D context unavailable for preview generation')
    hctx.drawImage(canvas, 0, 0, half.width, half.height)
    blob = await canvasToBlob(half, 'image/jpeg', 0.6)
    if (!blob || blob.size > MAX_PREVIEW_BYTES) {
      throw new Error('Preview exceeds 512 KiB after downscaling')
    }
  }

  return blobToDataUrl(blob)
}
