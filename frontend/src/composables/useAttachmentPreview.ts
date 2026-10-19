import { computed, inject, nextTick, provide, shallowRef, type InjectionKey } from 'vue'

export interface AttachmentTarget {
  scope: string
  path: string
  hash?: string
  imageUrl?: string
  name?: string
  registered?: boolean
}

type OpenAttachment = (target: AttachmentTarget, event?: MouseEvent) => void
const attachmentPreviewKey: InjectionKey<OpenAttachment> = Symbol('AttachmentPreview')

export function useAttachmentOpener() {
  return inject(attachmentPreviewKey, null)
}

/** Each Chat owns its preview stack; switching/deactivating the Chat discards it. */
export function provideAttachmentPreview() {
  const stack = shallowRef<AttachmentTarget[]>([])
  const target = computed(() => stack.value[stack.value.length - 1] ?? null)
  let trigger: HTMLElement | null = null
  const open: OpenAttachment = (item, event) => {
    if (!target.value) {
      trigger = event?.target instanceof Element
        ? event.target.closest<HTMLElement>('a, button') : null
      trigger ??= document.activeElement instanceof HTMLElement ? document.activeElement : null
    }
    stack.value = [...stack.value, item]
  }
  function close(restoreFocus = true) {
    const previous = trigger
    stack.value = []
    trigger = null
    if (restoreFocus) void nextTick(() => { if (previous?.isConnected) previous.focus({ preventScroll: true }) })
  }
  function back() {
    if (stack.value.length > 1) stack.value = stack.value.slice(0, -1)
  }
  provide(attachmentPreviewKey, open)
  return { target, canGoBack: computed(() => stack.value.length > 1), open, close, back }
}
