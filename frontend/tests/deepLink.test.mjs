import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import ts from 'typescript'

const source = readFileSync(
  new URL('../src/utils/deepLink.ts', import.meta.url),
  'utf8',
)
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2020 },
})
const { buildTabLink, buildTabShareText, parseTabDeepLink } = await import(
  `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
)

test('buildTabLink encodes the tab id into a query parameter on the current origin', () => {
  globalThis.window = {
    location: { origin: 'http://localhost:8173', pathname: '/' },
  }
  assert.equal(buildTabLink('abc-123'), 'http://localhost:8173/?tab=abc-123')
})

test('buildTabLink URL-encodes special characters in the tab id', () => {
  globalThis.window = {
    location: { origin: 'https://hub.example.com', pathname: '/app/' },
  }
  assert.equal(buildTabLink('a b&c'), 'https://hub.example.com/app/?tab=a%20b%26c')
})

test('parseTabDeepLink extracts the tab id from a query string', () => {
  assert.equal(parseTabDeepLink('?tab=abc-123'), 'abc-123')
})

test('parseTabDeepLink returns the first tab id when several are present', () => {
  assert.equal(parseTabDeepLink('?tab=first&tab=second'), 'first')
})

test('parseTabDeepLink returns null when there is no tab parameter', () => {
  assert.equal(parseTabDeepLink('?foo=bar'), null)
  assert.equal(parseTabDeepLink(''), null)
})

test('buildTabShareText puts the clean deep link on the first line', () => {
  globalThis.window = {
    location: { origin: 'http://localhost:8173', pathname: '/' },
  }
  const text = buildTabShareText('abc-123')
  assert.equal(text.split('\n')[0], 'http://localhost:8173/?tab=abc-123')
})

test('buildTabShareText hints at the paged stream events endpoint', () => {
  globalThis.window = {
    location: { origin: 'http://localhost:8173', pathname: '/' },
  }
  const text = buildTabShareText('abc-123')
  assert.match(
    text,
    /http:\/\/localhost:8173\/api\/tabs\/abc-123\/stream\/events\?since_sequence=-1&limit=5000/,
  )
  assert.match(text, /paged JSON/)
})

test('buildTabShareText URL-encodes the tab id in the events URL', () => {
  globalThis.window = {
    location: { origin: 'https://hub.example.com', pathname: '/app/' },
  }
  const text = buildTabShareText('a b&c')
  assert.match(text, /https:\/\/hub.example.com\/api\/tabs\/a%20b%26c\/stream\/events/)
})
