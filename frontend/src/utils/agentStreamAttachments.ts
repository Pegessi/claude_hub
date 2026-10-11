/**
 * Pure helpers for the structured composer's image attachment pipeline.
 *
 * Kept separate from ``useAgentStream`` so they can be unit-tested without
 * pulling in Vue's reactivity runtime.
 */

const SUPPORTED_IMAGE_MIME = /^image\/(png|jpeg|gif|webp)$/i
const MAX_IMAGE_BYTES = 8 * 1024 * 1024 // 8 MB

/**
 * Stored-image bounds; they must match the backend attachment cache
 * (``MAX_PREVIEW_EDGE`` and ``attachment_max_preview_bytes``).
 */
export const MAX_PREVIEW_EDGE = 8192
export const MAX_PREVIEW_BYTES = 8 * 1024 * 1024 // 8 MiB

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

// iOS Safari cannot draw into a canvas larger than 16,777,216 pixels.
const MAX_CANVAS_PIXELS = 4096 * 4096

// Near-lossless encodings tried in order when the original cannot be stored.
// A browser that cannot encode a type returns PNG instead, so results are
// accepted only when the blob has the requested type.
const REENCODE_STEPS: ReadonlyArray<readonly [string, number | undefined]> = [
  ['image/png', undefined],
  ['image/webp', 0.95],
  ['image/jpeg', 0.92],
  ['image/webp', 0.85],
  ['image/jpeg', 0.85],
]

async function encodeWithinCap(canvas: HTMLCanvasElement): Promise<Blob | null> {
  for (const [type, quality] of REENCODE_STEPS) {
    const blob = await canvasToBlob(canvas, type, quality)
    if (blob && blob.type === type && blob.size <= MAX_PREVIEW_BYTES) return blob
  }
  return null
}

/**
 * Produce the display image that the Chat stores for an attachment.
 *
 * An original within ``MAX_PREVIEW_BYTES`` and ``MAX_PREVIEW_EDGE`` is
 * returned unchanged (byte-identical, so GIF animation survives). Larger
 * images are scaled to fit ``MAX_PREVIEW_EDGE`` (and the canvas pixel limit
 * of mobile Safari) and re-encoded as PNG when it
 * fits, otherwise as high-quality WebP/JPEG; the size is halved only as a last
 * resort.
 *
 * The original ``data_url`` is always what the provider receives.
 *
 * @param file The original image file.
 * @param dataUrl Optional pre-read data URL of the original. When supplied,
 *   the file is not read again (single-read preparation).
 */
export async function generatePreviewDataUrl(file: File, dataUrl?: string): Promise<string> {
  const originalDataUrl = dataUrl ?? await fileToDataUrl(file)
  const img = await loadImage(originalDataUrl)

  const longestEdge = Math.max(img.naturalWidth, img.naturalHeight)
  if (file.size <= MAX_PREVIEW_BYTES && longestEdge <= MAX_PREVIEW_EDGE) {
    return originalDataUrl
  }

  const scale = Math.min(
    1,
    MAX_PREVIEW_EDGE / longestEdge,
    Math.sqrt(MAX_CANVAS_PIXELS / (img.naturalWidth * img.naturalHeight)),
  )
  let width = Math.max(1, Math.round(img.naturalWidth * scale))
  let height = Math.max(1, Math.round(img.naturalHeight * scale))
  for (let attempt = 0; attempt < 2; attempt += 1) {
    const canvas = document.createElement('canvas')
    canvas.width = width
    canvas.height = height
    const ctx = canvas.getContext('2d')
    if (!ctx) {
      throw new Error('Canvas 2D context unavailable for preview generation')
    }
    ctx.drawImage(img, 0, 0, width, height)
    const blob = await encodeWithinCap(canvas)
    if (blob) return blobToDataUrl(blob)
    width = Math.max(1, Math.round(width / 2))
    height = Math.max(1, Math.round(height / 2))
  }
  throw new Error('Image exceeds the 8 MiB stored-image limit after downscaling')
}
