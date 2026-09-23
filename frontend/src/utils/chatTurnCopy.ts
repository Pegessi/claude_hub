/**
 * Build the plain-text representation of one whole chat turn for the
 * "copy conversation" control.
 *
 * A turn is the user question followed by every assistant text block in
 * arrival order. Thinking, tool calls (input/result JSON), approvals, status
 * lines and internal events are intentionally excluded — only the text parts
 * rendered as assistant conversation bubbles are copied. Markdown is left as
 * authored; blocks are joined with blank lines.
 */
export interface TurnCopySource {
  userText: string
  parts: ReadonlyArray<{ kind: string; text?: string }>
}

export function buildTurnCopyText(turn: TurnCopySource): string {
  const sections: string[] = []

  const userText = turn.userText?.trim()
  if (userText) {
    sections.push(`User:\n${userText}`)
  }

  const assistantTexts = turn.parts
    .filter(
      (part): part is { kind: 'text'; text: string } =>
        part.kind === 'text'
        && typeof part.text === 'string'
        && part.text.trim().length > 0,
    )
    .map(part => part.text.trim())

  if (assistantTexts.length > 0) {
    sections.push(`Assistant:\n${assistantTexts.join('\n\n')}`)
  }

  return sections.join('\n\n')
}
