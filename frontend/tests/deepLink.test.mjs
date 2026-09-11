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
const { buildTabLink, parseTabDeepLink } = await import(
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
