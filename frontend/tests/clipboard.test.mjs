import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import ts from 'typescript'

const source = readFileSync(
  new URL('../src/utils/clipboard.ts', import.meta.url),
  'utf8',
)
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2020 },
})
const { writeClipboard } = await import(
  `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
)

function setNavigator(value) {
  Object.defineProperty(globalThis, 'navigator', {
    value,
    configurable: true,
    writable: true,
  })
}

function fakeDocument(execCommandResult) {
  const appended = []
  return {
    doc: {
      createElement: () => ({
        value: '',
        setAttribute: () => {},
        style: {},
        select: () => {},
      }),
      body: {
        appendChild: el => appended.push(el),
        removeChild: () => {},
      },
      execCommand: () => execCommandResult,
    },
    appended,
  }
}

test('writeClipboard uses navigator.clipboard.writeText when available', async () => {
  let written = null
  setNavigator({
    clipboard: {
      writeText: async value => {
        written = value
      },
    },
  })
  await writeClipboard('hello world')
  assert.equal(written, 'hello world')
})

test('writeClipboard falls back to execCommand when navigator.clipboard is absent', async () => {
  setNavigator({})
  let execCommandArg = null
  const { doc, appended } = fakeDocument(true)
  doc.execCommand = cmd => {
    execCommandArg = cmd
    return true
  }
  globalThis.document = doc
  await writeClipboard('fallback text')
  assert.equal(execCommandArg, 'copy')
  assert.equal(appended[0].value, 'fallback text')
})

test('writeClipboard throws when the execCommand fallback reports failure', async () => {
  setNavigator({})
  const { doc } = fakeDocument(false)
  globalThis.document = doc
  await assert.rejects(() => writeClipboard('x'), /copy failed/)
})
