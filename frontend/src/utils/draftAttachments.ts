import type { WorkspaceAttachmentCreate } from '@/types'

export interface DraftAttachment extends WorkspaceAttachmentCreate {
  id: string
  preview_url: string
  size_bytes: number
}

export function formatAttachmentSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

function draftAttachmentId() {
  if (typeof globalThis.crypto?.randomUUID === 'function') {
    return globalThis.crypto.randomUUID()
  }
  return `attachment-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`
}

function arrayBufferToBase64(buffer: ArrayBuffer) {
  const bytes = new Uint8Array(buffer)
  const chunkSize = 0x8000
  let binary = ''
  for (let index = 0; index < bytes.length; index += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(index, index + chunkSize))
  }
  return btoa(binary)
}

async function fileToImageDataUrl(file: File, mimeType: string): Promise<string> {
  const base64 = arrayBufferToBase64(await file.arrayBuffer())
  return `data:${mimeType};base64,${base64}`
}

function imageFilename(file: File) {
  if (file.name) return file.name
  if (file.type === 'image/jpeg') return 'clipboard.jpg'
  if (file.type === 'image/gif') return 'clipboard.gif'
  if (file.type === 'image/webp') return 'clipboard.webp'
  return 'clipboard.png'
}

function imageMimeType(file: File): string | null {
  const mimeType = file.type.trim().toLowerCase()
  if (mimeType.startsWith('image/')) {
    return mimeType === 'image/jpg' ? 'image/jpeg' : mimeType
  }

  const extension = file.name.split('.').pop()?.toLowerCase()
  if (extension === 'png') return 'image/png'
  if (extension === 'jpg' || extension === 'jpeg') return 'image/jpeg'
  if (extension === 'gif') return 'image/gif'
  if (extension === 'webp') return 'image/webp'
  return null
}

function normalizeImageDataUrl(dataUrl: string, mimeType: string) {
  if (dataUrl.startsWith(`data:${mimeType};base64,`)) return dataUrl
  return dataUrl.replace(/^data:[^;,]*(;base64,)/i, `data:${mimeType}$1`)
}

function clipboardImageFiles(event: ClipboardEvent): File[] {
  const clipboard = event.clipboardData
  if (!clipboard) return []

  const files: File[] = []
  const seen = new Set<string>()

  const addFile = (file: File | null) => {
    if (!file || !imageMimeType(file)) return
    const key = `${file.name}:${file.type}:${file.size}:${file.lastModified}`
    if (seen.has(key)) return
    seen.add(key)
    files.push(file)
  }

  for (const item of Array.from(clipboard.items || [])) {
    if (item.kind === 'file') {
      addFile(item.getAsFile())
    }
  }

  for (const file of Array.from(clipboard.files || [])) {
    addFile(file)
  }

  return files
}

function dataUrlFilename(mimeType: string, index: number) {
  if (mimeType === 'image/jpeg') return `clipboard-${index}.jpg`
  if (mimeType === 'image/gif') return `clipboard-${index}.gif`
  if (mimeType === 'image/webp') return `clipboard-${index}.webp`
  return `clipboard-${index}.png`
}

function imageDataUrlsFromText(value: string): string[] {
  const matches = value.match(/data:image\/(?:png|jpeg|jpg|gif|webp);base64,[A-Za-z0-9+/=]+/gi)
  return matches || []
}

function imageDataUrlKey(dataUrl: string): string | null {
  const match = dataUrl.match(/^data:(image\/(?:png|jpeg|jpg|gif|webp));base64,([A-Za-z0-9+/=]+)$/i)
  if (!match) return null
  const mimeType = match[1].toLowerCase() === 'image/jpg' ? 'image/jpeg' : match[1].toLowerCase()
  return `${mimeType}:${match[2]}`
}

function clipboardImageDataUrls(event: ClipboardEvent): string[] {
  const clipboard = event.clipboardData
  if (!clipboard) return []

  const urls = new Set<string>()
  const html = clipboard.getData('text/html')
  if (html) {
    const document = new DOMParser().parseFromString(html, 'text/html')
    for (const image of Array.from(document.images)) {
      if (image.src.startsWith('data:image/')) {
        urls.add(image.src)
      }
    }
    for (const url of imageDataUrlsFromText(html)) {
      urls.add(url)
    }
  }

  for (const url of imageDataUrlsFromText(clipboard.getData('text/plain'))) {
    urls.add(url)
  }

  return Array.from(urls)
}

function addDataUrlAttachments(
  target: DraftAttachment[],
  dataUrls: string[],
  seenDataUrlKeys: Set<string>
) {
  dataUrls.forEach((dataUrl, index) => {
    const match = dataUrl.match(/^data:(image\/(?:png|jpeg|jpg|gif|webp));base64,/i)
    if (!match) return
    const mimeType = match[1].toLowerCase() === 'image/jpg' ? 'image/jpeg' : match[1].toLowerCase()
    const normalizedDataUrl = dataUrl.replace(/^data:image\/jpg;/i, 'data:image/jpeg;')
    const key = imageDataUrlKey(normalizedDataUrl)
    if (key && seenDataUrlKeys.has(key)) return
    if (key) seenDataUrlKeys.add(key)
    target.push({
      id: draftAttachmentId(),
      filename: dataUrlFilename(mimeType, index + 1),
      mime_type: mimeType,
      data_url: normalizedDataUrl,
      preview_url: dataUrl,
      size_bytes: Math.floor((dataUrl.length - dataUrl.indexOf(',') - 1) * 0.75),
    })
  })
}

async function addImageAttachments(
  target: DraftAttachment[],
  files: File[],
  seenDataUrlKeys: Set<string>
) {
  for (const file of files) {
    const mimeType = imageMimeType(file)
    if (!mimeType) continue
    const dataUrl = normalizeImageDataUrl(await fileToImageDataUrl(file, mimeType), mimeType)
    const key = imageDataUrlKey(dataUrl)
    if (key && seenDataUrlKeys.has(key)) continue
    if (key) seenDataUrlKeys.add(key)
    target.push({
      id: draftAttachmentId(),
      filename: imageFilename(file),
      mime_type: mimeType,
      data_url: dataUrl,
      preview_url: dataUrl,
      size_bytes: file.size,
    })
  }
}

export async function handleAttachmentPaste(event: ClipboardEvent, target: DraftAttachment[]) {
  const files = clipboardImageFiles(event)
  const dataUrls = clipboardImageDataUrls(event)
  if (files.length === 0 && dataUrls.length === 0) return
  event.preventDefault()
  const seenDataUrlKeys = new Set<string>()
  await addImageAttachments(target, files, seenDataUrlKeys)
  addDataUrlAttachments(target, dataUrls, seenDataUrlKeys)
}

export function removeDraftAttachment(target: DraftAttachment[], attachment: DraftAttachment) {
  const index = target.findIndex(item => item.id === attachment.id)
  if (index >= 0) {
    URL.revokeObjectURL(target[index].preview_url)
    target.splice(index, 1)
  }
}

export function resetDraftAttachments(target: DraftAttachment[]) {
  for (const attachment of target) {
    URL.revokeObjectURL(attachment.preview_url)
  }
  target.splice(0)
}

export function serializeDraftAttachments(target: DraftAttachment[]): WorkspaceAttachmentCreate[] {
  return target.map(({ filename, mime_type, data_url }) => ({ filename, mime_type, data_url }))
}
