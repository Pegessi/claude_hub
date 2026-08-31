import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const terminalPane = readFileSync(
  new URL('../src/components/TerminalPane.vue', import.meta.url),
  'utf8',
)

test('Paseo keeps Raw mounted behind an opacity boundary', () => {
  // ttyd's active iframe explicitly sets visibility:visible, so visibility on
  // a Vue parent is not a sufficient hiding boundary. This assertion guards
  // the exclusive same-source view contract without needing a browser iframe.
  const hiddenRule = terminalPane.match(/\.pane-terminal\.is-hidden\s*\{([\s\S]*?)\n\}/)
  assert.ok(hiddenRule, 'the mounted Raw wrapper must have a hidden rule')
  assert.match(hiddenRule[1], /opacity:\s*0/)
  assert.match(hiddenRule[1], /pointer-events:\s*none/)
  assert.match(hiddenRule[1], /z-index:\s*0/)
})

test('Paseo has fixed visible chrome and a peer structured wrapper', () => {
  assert.match(terminalPane, /class="pane-header pane-session-header"/)
  assert.match(terminalPane, /class="pane-structured"/)
  assert.match(terminalPane, />\s*Terminal\s*<\/button>/)
  assert.match(terminalPane, />\s*Paseo\s*<\/button>/)
})
