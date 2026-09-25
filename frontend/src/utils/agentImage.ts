/**
 * Provider-independent recognition of an agent's "view an image" tool call.
 *
 * Different agents surface the same action under different tool names/args:
 *
 *  - Codex and TraeX (shared app-server family): the transcript normalizer
 *    emits a ``view_image`` tool call with ``args.path`` (see backend
 *    ``codex_jsonl.py`` ``imageView`` mapping).
 *  - Claude Code: there is no dedicated image-view tool. The model reads an
 *    image with its generic ``Read`` tool via ``args.file_path``; only treat
 *    it as an image when the path has an image suffix (the backend still
 *    validates magic bytes, so the suffix is purely a UI hint).
 *
 * Anything not recognized here falls back to the generic tool card.
 */

/** Tool names that unconditionally mean "display this local image". */
export const AGENT_IMAGE_TOOL_NAMES = new Set(['view_image'])

/**
 * Image suffixes the backend agent-image endpoint is willing to serve (its
 * magic-byte allowlist). Kept in sync with the backend PNG/JPEG/GIF/WebP
 * whitelist so we never build a URL guaranteed to 404.
 */
const IMAGE_PATH_SUFFIX = /\.(?:png|jpe?g|gif|webp)$/i

function nonEmptyPath(value: unknown): string | null {
  return typeof value === 'string' && value.trim().length > 0 ? value : null
}

/**
 * Return the local image path an image-viewing tool call references, or
 * ``null`` when the call is not an image view (the renderer then uses the
 * generic tool card).
 *
 * @param name The normalized tool call name from ``tool_call_started``.
 * @param args The structured tool args object from the same event.
 */
export function agentImagePathFromTool(
  name: string,
  args: unknown,
): string | null {
  if (typeof name !== 'string' || name.length === 0) return null
  const record = args && typeof args === 'object' ? args as Record<string, unknown> : {}

  if (AGENT_IMAGE_TOOL_NAMES.has(name)) {
    return nonEmptyPath(record.path)
  }

  // Claude Code reads images with the Read tool. Only classify image-shaped
  // paths; a Read of source code stays on the generic tool card.
  if (name === 'Read') {
    const filePath = nonEmptyPath(record.file_path)
    if (filePath !== null && IMAGE_PATH_SUFFIX.test(filePath.trim())) {
      return filePath
    }
  }

  return null
}
