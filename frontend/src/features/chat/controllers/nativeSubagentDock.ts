import { inject, shallowRef, type InjectionKey, type ShallowRef } from 'vue'

import type { NativeSubagentRow } from '../presentation/nativeSubagentRows'

/**
 * Per-pane surface state for native sub-agent rows.
 *
 * The rows are derived from the pane's Chat timeline, which only the Chat body
 * owns (`StructuredPane`), while the control that lists them lives in the pane
 * chrome (`TerminalPane`). The chrome therefore owns this small state object,
 * publishes it down through `provide`, and the Chat body fills in the rows and
 * renders the detail window (it is the only subtree that holds the timeline and
 * image owners that window needs).
 *
 * It is deliberately not a store: the rows are a projection of one pane's
 * timeline, and they die with that pane. Seeding them in a finite ref keeps
 * Vue's dependency graph one-way (body writes, chrome reads) instead of
 * reaching into a child's state from above.
 */
export interface NativeSubagentDock {
  rows: ShallowRef<NativeSubagentRow[]>
  /** The row whose detail window is open, or ``null``. */
  openRow: ShallowRef<NativeSubagentRow | null>
  publish(rows: NativeSubagentRow[]): void
  /** `opener` is the row that was clicked; `fallback` is the chrome control to
   *  return to when that row is gone by the time the window closes. */
  open(row: NativeSubagentRow, opener?: HTMLElement | null, fallback?: HTMLElement | null): void
  close(): void
}

export function createNativeSubagentDock(): NativeSubagentDock {
  const rows = shallowRef<NativeSubagentRow[]>([])
  const openRow = shallowRef<NativeSubagentRow | null>(null)
  let opener: HTMLElement | null = null
  let fallback: HTMLElement | null = null
  return {
    rows,
    openRow,
    publish(next) {
      rows.value = next
      // A row that left the transcript (tab switched, history reset) cannot
      // stay open: its window would have nothing to resolve. This is a data
      // change, not a dismissal, so it must not move focus.
      const current = openRow.value
      if (current && !next.some(row => row.key === current.key)) {
        openRow.value = null
        opener = null
        fallback = null
      }
    },
    open(row, source, chromeControl) {
      opener = source ?? null
      fallback = chromeControl ?? null
      openRow.value = row
    },
    close() {
      openRow.value = null
      // Only a dismissal restores focus: to the row that opened the window
      // while it is still mounted (the wide-pane rail stays open), else to the
      // chrome control the reader came from.
      const targets = [opener, fallback]
      opener = null
      fallback = null
      for (const target of targets) {
        if (target?.isConnected) {
          target.focus()
          return
        }
      }
    },
  }
}

export const nativeSubagentDockKey: InjectionKey<NativeSubagentDock> = Symbol('nativeSubagentDock')

export function useNativeSubagentDock(): NativeSubagentDock | null {
  return inject(nativeSubagentDockKey, null)
}
