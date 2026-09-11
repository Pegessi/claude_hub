import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import ts from 'typescript'

// ---------------------------------------------------------------------------
// formatClockTime is what keeps the transcript from ever rendering "NaN:NaN":
// a turn with no known time must produce an empty label the template can skip.
// ---------------------------------------------------------------------------

const source = await readFile(
  new URL('../src/utils/duration.ts', import.meta.url),
  'utf8',
)
const js = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2020 },
}).outputText
const { formatClockTime } = await import(
  `data:text/javascript;base64,${Buffer.from(js).toString('base64')}`
)

test('a missing or unparseable timestamp yields no label', () => {
  assert.equal(formatClockTime(null), '')
  assert.equal(formatClockTime(undefined), '')
  assert.equal(formatClockTime(''), '')
  assert.equal(formatClockTime('not a date'), '')
})

test('hours and minutes are zero-padded', () => {
  // Local time, so build the expectation from the same Date the source uses.
  const iso = '2026-01-01T05:04:00Z'
  const d = new Date(iso)
  const expected = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
  assert.equal(formatClockTime(iso), expected)
  assert.equal(formatClockTime(iso).length, 5, 'always HH:MM')
})
