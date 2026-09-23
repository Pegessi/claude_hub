import assert from 'node:assert/strict'
import test from 'node:test'

import {
  isPtyGateway,
  remoteInteractive,
  remoteProfileBadge,
} from '../src/utils/remoteProfiles.ts'

const normal = { id: 'mac_mini', name: 'mac_mini', ssh_host: 'mac-mini.local', port: 22 }
const gateway = { id: 'merlin_dev', name: 'merlin_dev', ssh_host: 'merlin_dev', port: 22, transport: 'pty_gateway' }
const browseOnly = { id: 'dead', name: 'dead', ssh_host: 'merlin_dev', port: 22, transport: 'pty_gateway', interactive: false }
const legacyStdinShell = { id: 'old', name: 'old', ssh_host: 'old', port: 22, stdin_shell: true }

test('isPtyGateway keys off the explicit transport', () => {
  assert.equal(isPtyGateway(gateway), true)
  assert.equal(isPtyGateway(normal), false)
  assert.equal(isPtyGateway(browseOnly), true)
  assert.equal(isPtyGateway(null), false)
})

test('remoteInteractive allows normal and gateway, rejects only explicit false', () => {
  assert.equal(remoteInteractive(normal), true)
  assert.equal(remoteInteractive(gateway), true)
  assert.equal(remoteInteractive(browseOnly), false)
  // A legacy stdin_shell-only flag must NOT be treated as non-interactive now.
  assert.equal(remoteInteractive(legacyStdinShell), true)
  assert.equal(remoteInteractive(undefined), true)
})

test('remoteProfileBadge labels browse-only vs gateway vs normal', () => {
  assert.equal(remoteProfileBadge(browseOnly), ' · listing only')
  assert.equal(remoteProfileBadge(gateway), ' · PTY gateway')
  assert.equal(remoteProfileBadge(normal), '')
})
