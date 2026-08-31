import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

const source = await readFile(new URL('../src/utils/textReveal.ts', import.meta.url), 'utf8')
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 },
})
const reveal = await import(
  `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
)

test('new history renders whole while later growth is paced', () => {
  const initial = reveal.beginTextReveal('hello')
  assert.equal(reveal.visibleRevealedText(initial), 'hello')
  const retargeted = reveal.retargetTextReveal(initial, 'hello world')
  assert.equal(retargeted.revealed, 5)
  const advanced = reveal.advanceTextReveal(retargeted, 1000 / 60)
  assert.ok(advanced.revealed > 5)
  assert.ok(advanced.revealed < 11)
})

test('completion flushes all authoritative text', () => {
  const state = reveal.retargetTextReveal(reveal.beginTextReveal('a'), 'abcdef')
  assert.equal(reveal.completeTextReveal(state).revealed, 6)
})

test('frame pacing is capped near 60Hz', () => {
  assert.equal(reveal.nextTextRevealFrame(100, 105), null)
  const frame = reveal.nextTextRevealFrame(100, 117)
  assert.ok(frame)
  assert.ok(frame.elapsedMs >= 16)
})

test('paced reveal never splits an extended grapheme cluster', () => {
  const family = '\u{1F468}\u200D\u{1F469}\u200D\u{1F467}'
  const text = `${family}!`
  for (let index = 1; index < family.length; index += 1) {
    assert.equal(reveal.clampToSafeRevealBoundary(text, index), 0)
  }
  assert.equal(reveal.clampToSafeRevealBoundary(text, family.length), family.length)
})

test('streaming holds incomplete arrival fragments until the cluster is complete', () => {
  const halfFlag = 'flags: \u{1F1EC}'
  const halfState = reveal.beginTextReveal(halfFlag)
  assert.equal(reveal.visibleRevealedText(halfState, { streaming: true }), 'flags: ')
  assert.equal(reveal.visibleRevealedText(halfState, { streaming: false }), halfFlag)

  const wholeFlag = 'flags: \u{1F1EC}\u{1F1E7}'
  assert.equal(
    reveal.visibleRevealedText(reveal.beginTextReveal(wholeFlag), { streaming: true }),
    wholeFlag,
  )
  assert.equal(
    reveal.visibleRevealedText(
      reveal.beginTextReveal('family: \u{1F468}\u200D'),
      { streaming: true },
    ),
    'family: \u{1F468}',
  )
})

test('completion releases a dangling provider fragment instead of hiding data forever', () => {
  const loneHigh = `broken: ${String.fromCharCode(0xd83d)}`
  const state = reveal.beginTextReveal(loneHigh)
  assert.equal(reveal.visibleRevealedText(state, { streaming: true }), 'broken: ')
  assert.equal(
    reveal.visibleRevealedText(reveal.completeTextReveal(state), { streaming: false }),
    loneHigh,
  )
})
