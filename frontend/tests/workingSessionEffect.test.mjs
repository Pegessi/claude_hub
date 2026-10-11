import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import {
  WORKING_BEAM_EDGE_SHARE,
  WORKING_BEAM_LEAD,
  WORKING_BEAM_TAIL,
  beamPassFraction,
  createWorkingBeamLayers,
  createWorkingStarfield,
} from '../src/utils/workingSessionEffect.ts'

const effect = readFileSync(
  new URL('../src/components/ChatSidebarWorkingEffect.vue', import.meta.url),
  'utf8',
)

test('the beam crosses the top edge left to right and the bottom edge right to left', () => {
  const top = [0, 0.25, 0.5, 0.75, 1].map(x => beamPassFraction(x, 'top'))
  const bottom = [0, 0.25, 0.5, 0.75, 1].map(x => beamPassFraction(x, 'bottom'))
  for (let index = 1; index < top.length; index += 1) {
    assert.ok(top[index] > top[index - 1])
    assert.ok(bottom[index] < bottom[index - 1])
  }
  assert.equal(top[0], 0)
  assert.equal(top.at(-1), WORKING_BEAM_EDGE_SHARE)
  assert.equal(bottom.at(-1), 0.5)
  assert.equal(bottom[0], 0.5 + WORKING_BEAM_EDGE_SHARE)
  for (const pass of [...top, ...bottom]) assert.ok(pass >= 0 && pass < 1)
})

test('each star joins the beam pass of the half of the row it sits in', () => {
  const stars = createWorkingStarfield('8b1f3c4e-tab')
  assert.equal(stars.filter(star => star.kind === 'dust').length, 14)
  assert.equal(stars.filter(star => star.kind === 'glint').length, 2)
  for (const star of stars) {
    assert.ok(star.x >= 4 && star.x <= 96, `x=${star.x}`)
    assert.ok(star.y > 10 && star.y < 90, `y=${star.y}`)
    const column = (star.x - 4) / 92
    const edge = star.y < 50 ? 'top' : 'bottom'
    assert.ok(Math.abs(star.pass - beamPassFraction(column, edge)) < 0.001)
  }
})

test('star fields are stable per row and differ between rows', () => {
  assert.deepEqual(createWorkingStarfield('tab-a'), createWorkingStarfield('tab-a'))
  assert.notDeepEqual(createWorkingStarfield('tab-a'), createWorkingStarfield('tab-b'))
})

test('stars spread across the row width instead of clumping', () => {
  const dust = createWorkingStarfield('spread').filter(star => star.kind === 'dust')
  const xs = dust.map(star => star.x).sort((a, b) => a - b)
  for (let index = 1; index < xs.length; index += 1) {
    assert.ok(xs[index] - xs[index - 1] < 14, `gap ${xs[index - 1]} -> ${xs[index]}`)
  }
})

test('beam, light pool and stars share one orbit clock', () => {
  assert.match(effect, /\.working-effect__orbit \{[^}]*animation: working-effect-orbit var\(--working-orbit\) linear infinite/)
  assert.match(effect, /\.working-effect__pool \{[^}]*animation: working-effect-pool var\(--working-orbit\) linear infinite/)
  assert.match(effect, /animation-duration: var\(--working-orbit\);/)
  assert.match(effect, /animation-delay: calc\(var\(--working-orbit\) \* \(var\(--star-pass\) - 1\.03\)\);/)
  const share = `${WORKING_BEAM_EDGE_SHARE * 100}%`
  const returnShare = `${(0.5 + WORKING_BEAM_EDGE_SHARE) * 100}%`
  const pool = effect.match(/@keyframes working-effect-pool \{([\s\S]*?)\n\}/)[1]
  assert.match(pool, new RegExp(`${share.replace('.', '\\.')} \\{ transform: translate\\(100%, 0\\); \\}`))
  assert.match(pool, new RegExp(`${returnShare.replace('.', '\\.')} \\{ transform: translate\\(0, 100%\\); \\}`))
})

test('beam layers nest around one peak so the pulse fades out at both ends', () => {
  const layers = createWorkingBeamLayers().map(dasharray => {
    const [lead, gap, dash, rest] = dasharray.split(' ').map(Number)
    assert.equal(lead, 0)
    assert.ok(Math.abs(gap + dash + rest - 100) < 1e-6, dasharray)
    return { start: gap, end: gap + dash }
  })
  assert.equal(layers.length, 12)
  assert.equal(layers[0].start, 0)
  assert.equal(layers[0].end, WORKING_BEAM_TAIL + WORKING_BEAM_LEAD)
  for (let index = 0; index < layers.length; index += 1) {
    const layer = layers[index]
    assert.ok(layer.start < WORKING_BEAM_TAIL && layer.end > WORKING_BEAM_TAIL)
    if (index > 0) {
      assert.ok(layer.start > layers[index - 1].start)
      assert.ok(layer.end < layers[index - 1].end)
    }
  }
})

test('the pulse peak rides the orbit phase without a solid head segment', () => {
  assert.match(effect, new RegExp(`stroke-dashoffset: ${WORKING_BEAM_TAIL};`))
  assert.match(effect, new RegExp(`@keyframes working-effect-orbit \\{\\s*to \\{ stroke-dashoffset: ${WORKING_BEAM_TAIL - 100}; \\}`))
  assert.match(effect, /v-for="\(dasharray, index\) in BEAM_LAYERS"[\s\S]*?:stroke-dasharray="dasharray"/)
  assert.doesNotMatch(effect, /working-effect__head|working-effect__trail/)
})

test('the working light is decorative and disappears for reduced motion', () => {
  assert.match(effect, /class="working-effect"\s+aria-hidden="true"/)
  assert.match(effect, /pointer-events: none;/)
  assert.match(effect, /@media \(prefers-reduced-motion: reduce\) \{\s*\.working-effect \{\s*display: none;/)
  assert.doesNotMatch(effect, /mask-composite|clip-path|offset-path/)
})
