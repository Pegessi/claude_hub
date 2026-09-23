import type { RemoteProfile } from '../types'

/**
 * Capability model for remote SSH profiles.
 *
 * - `normal` hosts run `ssh host 'cmd'` and interactive tabs directly.
 * - `pty_gateway` hosts (e.g. merlin_dev) swallow the ssh argv/non-tty channel
 *   but expose a fully interactive PTY; the Hub drives them with a typed
 *   handshake. Both are interactive.
 * - Only an explicit `interactive === false` marks a target browse-only.
 *
 * The old `stdin_shell` flag conflated "argv is swallowed" with "no TTY" and
 * wrongly disabled Terminal/agents on PTY gateways. New UI must gate on
 * `remoteInteractive()` instead.
 */

export function isPtyGateway(profile: RemoteProfile | null | undefined): boolean {
  return profile?.transport === 'pty_gateway'
}

export function remoteInteractive(profile: RemoteProfile | null | undefined): boolean {
  return profile?.interactive !== false
}

/** Short suffix shown after the profile name in a <select>. */
export function remoteProfileBadge(profile: RemoteProfile | null | undefined): string {
  if (!profile) return ''
  if (!remoteInteractive(profile)) return ' · listing only'
  if (isPtyGateway(profile)) return ' · PTY gateway'
  return ''
}
