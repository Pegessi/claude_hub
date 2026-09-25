import test from 'node:test'
import assert from 'node:assert/strict'

import { agentImagePathFromTool, AGENT_IMAGE_TOOL_NAMES } from '../src/utils/agentImage.ts'

test('Codex/TraeX view_image maps its args.path', () => {
  const p = '/Users/a/work/tasks/run/gpu.png'
  assert.equal(agentImagePathFromTool('view_image', { path: p }), p)
})

test('view_image accepts a cwd-relative image path', () => {
  assert.equal(agentImagePathFromTool('view_image', { path: 'out/chart.jpg' }), 'out/chart.jpg')
})

test('view_image with missing/blank/non-string path is not an image', () => {
  assert.equal(agentImagePathFromTool('view_image', {}), null)
  assert.equal(agentImagePathFromTool('view_image', { path: '' }), null)
  assert.equal(agentImagePathFromTool('view_image', { path: '   ' }), null)
  assert.equal(agentImagePathFromTool('view_image', { path: 42 }), null)
  assert.equal(agentImagePathFromTool('view_image', null), null)
})

test('Claude Read of an image suffix is treated as an image', () => {
  const p = '/work/diagram.PNG'
  assert.equal(agentImagePathFromTool('Read', { file_path: p }), p)
  for (const ext of ['png', 'jpeg', 'jpg', 'gif', 'webp']) {
    assert.equal(agentImagePathFromTool('Read', { file_path: `/x/a.${ext}` }), `/x/a.${ext}`)
  }
})

test('Claude Read of a non-image file falls back (generic tool card)', () => {
  assert.equal(agentImagePathFromTool('Read', { file_path: '/work/src/main.ts' }), null)
  assert.equal(agentImagePathFromTool('Read', { file_path: '/work/README' }), null)
  assert.equal(agentImagePathFromTool('Read', {}), null)
})

test('unrelated tools are not image views', () => {
  assert.equal(agentImagePathFromTool('Bash', { command: 'ls' }), null)
  assert.equal(agentImagePathFromTool('Edit', { file_path: '/x/a.png', content: 'x' }), null)
  assert.equal(agentImagePathFromTool('web_search', { query: 'png' }), null)
})

test('the whitelist contains the normalized view_image name', () => {
  assert.ok(AGENT_IMAGE_TOOL_NAMES.has('view_image'))
})
