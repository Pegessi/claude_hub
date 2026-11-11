<template>
  <dialog
    ref="dialog"
    class="appearance-dialog"
    aria-labelledby="appearance-title"
    aria-describedby="appearance-description"
    @cancel.prevent="close"
    @close="close"
    @click="onBackdrop"
  >
    <header class="appearance-header">
      <div>
        <h2 id="appearance-title">
          Make yourself at home
        </h2>
        <p id="appearance-description">
          Quiet by default. Expressive by choice.
        </p>
      </div>
      <button
        type="button"
        class="appearance-done"
        autofocus
        @click="close"
      >
        Done
      </button>
    </header>
    <div class="appearance-body">
      <fieldset class="appearance-fieldset">
        <legend>Appearance</legend>
        <div class="appearance-modes">
          <label
            v-for="option in modes"
            :key="option.id"
            class="appearance-mode"
          >
            <input
              type="radio"
              name="appearance-mode"
              :value="option.id"
              :checked="app.appearanceMode === option.id"
              @change="app.setAppearance(option.id)"
            >
            <span>{{ option.name }}</span>
          </label>
        </div>
        <p class="appearance-hint">
          {{ app.appearanceMode === 'system' ? `Following your device · ${app.colorScheme}` : 'Choose a mode, then find your palette.' }}
        </p>
      </fieldset>
      <fieldset class="appearance-fieldset">
        <legend>Theme</legend>
        <div class="appearance-palettes">
          <label
            v-for="palette in THEME_PALETTES"
            :key="palette.id"
            class="appearance-card"
          >
            <input
              type="radio"
              name="theme-palette"
              :value="palette.id"
              :checked="app.themePalette === palette.id"
              @change="app.setAppearance(app.appearanceMode, palette.id)"
            >
            <span
              class="theme-preview"
              :data-theme="app.colorScheme"
              :data-palette="palette.id"
              aria-hidden="true"
            >
              <span class="theme-preview-sidebar"><i /><i /><i /></span>
              <span class="theme-preview-content"><span class="theme-preview-title">{{ palette.sample }}</span><i /><b /><i /></span>
            </span>
            <span class="appearance-card-title">{{ palette.name }}<span
              class="appearance-check"
              aria-hidden="true"
            >✓</span></span>
            <span class="appearance-card-description">{{ palette.description }}</span>
          </label>
        </div>
      </fieldset>
    </div>
    <footer
      class="appearance-footer"
      aria-live="polite"
    >
      <span
        v-if="app.themePersistenceError"
        role="alert"
      >Applied here, but this browser could not save your preference.</span>
      <span v-else>{{ app.themePaletteName }} · {{ app.appearanceMode === 'system' ? 'System' : app.colorScheme === 'dark' ? 'Dark' : 'Light' }} · Saved on this browser</span>
    </footer>
  </dialog>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { useAppStore } from '@/stores/appStore'
import { THEME_PALETTES, type AppearanceMode } from '@/features/app-shell/themePreference'

const app = useAppStore()
const dialog = ref<HTMLDialogElement | null>(null)
const modes: { id: AppearanceMode; name: string }[] = [
  { id: 'system', name: 'System' }, { id: 'light', name: 'Light' }, { id: 'dark', name: 'Dark' },
]
const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null
// A mobile menu closes when it launches this sheet; return to its visible
// summary, never to the now-hidden menu item.
const returnFocus = opener?.closest('details')?.querySelector('summary') ?? opener
function close() { app.appearanceVisible = false }
function onBackdrop(event: MouseEvent) {
  if (event.target !== dialog.value || !dialog.value) return
  const bounds = dialog.value.getBoundingClientRect()
  if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) close()
}
onMounted(() => { dialog.value?.showModal() })
onUnmounted(() => {
  dialog.value?.close()
  if (returnFocus instanceof HTMLElement && returnFocus.isConnected) returnFocus.focus({ preventScroll: true })
})
</script>

<style scoped>
.appearance-dialog {
  margin: auto;
  padding: 0;
  width: min(660px, calc(100vw - 32px));
  max-width: none;
  max-height: calc(100dvh - 48px);
  color: var(--ch-color-text);
  background: var(--ch-color-surface);
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-overlay);
  box-shadow: var(--ch-shadow-lg);
}
.appearance-dialog[open] { display: flex; flex-direction: column; }
.appearance-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; padding: 24px 24px 18px; flex-shrink: 0; }
.appearance-header h2 { font-size: 20px; line-height: 1.3; font-weight: 600; letter-spacing: -0.03em; }
.appearance-header p { margin-top: 6px; color: var(--ch-color-text-muted); font-size: 13px; line-height: 1.5; }
.appearance-done { min-width: 56px; min-height: 44px; padding: 0 12px; border: 1px solid var(--ch-color-border-strong); border-radius: var(--ch-radius-lg); color: var(--ch-color-text); background: var(--ch-color-surface-control); cursor: pointer; }
.appearance-done:hover { background: var(--ch-color-surface-control-hover); }
.appearance-done:focus-visible { outline: 2px solid var(--ch-color-accent-ring); outline-offset: 3px; }
.appearance-body { overflow-y: auto; overscroll-behavior: contain; padding: 0 24px 24px; min-height: 0; }
.appearance-fieldset { min-width: 0; border: 0; padding: 0; }
.appearance-fieldset + .appearance-fieldset { margin-top: 22px; }
.appearance-fieldset legend { margin-bottom: 10px; color: var(--ch-color-text-muted); font-size: 12px; font-weight: 600; }
.appearance-modes { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 4px; padding: 4px; border: 1px solid var(--ch-color-border); border-radius: 12px; background: var(--ch-color-main-bg); }
.appearance-mode, .appearance-card { position: relative; cursor: pointer; min-width: 0; }
.appearance-mode input, .appearance-card input { position: absolute; width: 1px; height: 1px; opacity: 0; }
.appearance-mode > span { min-height: 44px; display: flex; align-items: center; justify-content: center; border: 1px solid transparent; border-radius: 8px; color: var(--ch-color-text-muted); font-size: 13px; }
.appearance-mode input:checked + span { border-color: var(--ch-color-border-selected); background: var(--ch-color-surface-selected); color: var(--ch-color-text-selected); }
.appearance-mode input:focus-visible + span { outline: 2px solid var(--ch-color-accent-ring); outline-offset: 2px; }
.appearance-hint { margin-top: 8px; color: var(--ch-color-text-subtle); font-size: 12px; line-height: 1.5; }
.appearance-palettes { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 16px 12px; }
.appearance-card { display: flex; flex-direction: column; gap: 5px; }
.appearance-card-title { display: flex; align-items: center; justify-content: space-between; gap: 4px; font-size: 13px; font-weight: 600; }
.appearance-card-description { font-size: 11px; line-height: 1.45; color: var(--ch-color-text-muted); }
.appearance-check { color: var(--ch-color-accent); visibility: hidden; }
.appearance-card input:checked ~ .appearance-card-title .appearance-check { visibility: visible; }
.appearance-card input:checked + .theme-preview { outline: 2px solid var(--ch-color-accent-ring); outline-offset: 2px; }
.appearance-card input:focus-visible + .theme-preview { outline: 3px solid var(--ch-color-accent-ring); outline-offset: 3px; }
.theme-preview { display: flex; height: 82px; overflow: hidden; border: 1px solid var(--ch-color-border-strong); border-radius: 8px; background: var(--ch-color-main-bg); margin: 3px 3px 5px; }
.theme-preview-sidebar { flex: 0 0 27%; padding: 12px 6px; display: flex; flex-direction: column; gap: 7px; background: var(--ch-color-sidebar-bg); border-right: 1px solid var(--ch-color-border); }
.theme-preview i { display: block; height: 3px; border-radius: 2px; background: var(--ch-color-text-subtle); opacity: 0.5; }
.theme-preview-sidebar i:first-child { height: 5px; background: var(--ch-color-accent); opacity: 1; }
.theme-preview-content { flex: 1; padding: 13px 9px; display: flex; flex-direction: column; gap: 7px; }
.theme-preview-title { font-family: var(--ch-font-display); font-style: var(--ch-heading-style); font-size: 12px; line-height: 1; font-weight: 800; color: var(--ch-color-text); }
.theme-preview-content i:last-child { width: 70%; }
.theme-preview-content b { align-self: flex-end; width: 58%; height: 15px; border-radius: 4px; background: var(--ch-color-message-user-bg); }
.appearance-footer { padding: 14px 24px; flex-shrink: 0; border-top: 1px solid var(--ch-color-border-muted); color: var(--ch-color-text-muted); font-size: 12px; line-height: 1.5; }
@media (max-width: 640px) {
  .appearance-dialog[open] { inset: auto 0 0; margin: 0; width: 100%; max-height: calc(100dvh - 16px - env(safe-area-inset-top, 0px)); border-radius: var(--ch-radius-overlay) var(--ch-radius-overlay) 0 0; border-width: 1px 0 0; box-shadow: var(--ch-shadow-sheet); animation-name: ch-sheet-in; }
  .appearance-header { padding: 20px 20px 16px; }
  .appearance-header h2 { font-size: 19px; }
  .appearance-body { padding: 0 20px 20px; }
  .appearance-palettes { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px 14px; }
  .appearance-footer { padding: 12px 20px calc(16px + env(safe-area-inset-bottom, 0px)); }
}
</style>
