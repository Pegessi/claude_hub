/**
 * Remembered "Plan first" preference for new Goals.
 *
 * The preference is a UI convenience only: the request still carries an
 * explicit `plan_first`, and storage failures must never block a Goal.
 */

const STORAGE_KEY = 'claude-hub.goal-plan-first'

export function readPlanFirstPreference(storage: Pick<Storage, 'getItem'> | null | undefined): boolean {
  try {
    const raw = storage?.getItem(STORAGE_KEY)
    // Absent means the option defaults on for a first-time user.
    return raw === null || raw === undefined ? true : raw === 'true'
  } catch {
    return true
  }
}

export function writePlanFirstPreference(
  storage: Pick<Storage, 'setItem'> | null | undefined,
  planFirst: boolean,
): void {
  try {
    storage?.setItem(STORAGE_KEY, planFirst ? 'true' : 'false')
  } catch {
    // Private-mode or quota failures keep the in-page choice only.
  }
}

/** The browser store, or null where localStorage is unavailable. */
export function browserStorage(): Storage | null {
  try {
    return typeof globalThis.localStorage === 'undefined' ? null : globalThis.localStorage
  } catch {
    return null
  }
}
