<template>
  <Teleport to="body">
    <dialog
      ref="dialog"
      class="attachment-preview-dialog"
      aria-label="Attachment preview"
      @cancel.prevent="requestClose"
      @click="onBackdrop"
      @keydown.stop
    >
      <AttachmentViewer
        v-if="target"
        :target="target"
        :can-go-back="canGoBack"
        closable
        @close="requestClose"
        @back="$emit('back')"
      />
    </dialog>
  </Teleport>
</template>

<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import AttachmentViewer from '@/components/AttachmentViewer.vue'
import type { AttachmentTarget } from '@/composables/useAttachmentPreview'

const props = defineProps<{ target: AttachmentTarget | null; canGoBack: boolean }>()
const emit = defineEmits<{ close: []; back: [] }>()
const dialog = ref<HTMLDialogElement | null>(null)
let marker = ''
let ownsHistory = false
let closing = false

function onPopState() {
  if (!ownsHistory || window.history.state?.attachmentPreview === marker) return
  ownsHistory = false
  closing = false
  if (dialog.value?.open) dialog.value.close()
  emit('close')
}
function requestClose() {
  if (closing) return
  if (ownsHistory && window.history.state?.attachmentPreview === marker) {
    closing = true
    window.history.back()
  } else {
    ownsHistory = false
    if (dialog.value?.open) dialog.value.close()
    emit('close')
  }
}
function onBackdrop(event: MouseEvent) {
  if (!dialog.value || event.target !== dialog.value) return
  const bounds = dialog.value.getBoundingClientRect()
  if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) requestClose()
}
function release() {
  if (dialog.value?.open) dialog.value.close()
  const goBack = ownsHistory && window.history.state?.attachmentPreview === marker
  ownsHistory = false
  closing = false
  if (goBack) window.history.back()
}
window.addEventListener('popstate', onPopState)
watch(() => Boolean(props.target), (open) => {
  if (!open) { release(); return }
  dialog.value?.showModal()
  if (!ownsHistory) {
    // A previous preview entry can remain in Forward history after closing.
    marker = `attachment-${Date.now()}-${Math.random().toString(36).slice(2)}`
    window.history.pushState({ ...window.history.state, attachmentPreview: marker }, '')
    ownsHistory = true
  }
}, { flush: 'post' })
onBeforeUnmount(() => {
  release()
  window.removeEventListener('popstate', onPopState)
})
</script>

<style scoped>
.attachment-preview-dialog { padding: 0; border: 1px solid var(--ch-color-border); border-radius: 16px; margin: auto; width: min(1120px, calc(100vw - 48px)); height: min(900px, calc(100dvh - 48px)); max-width: none; max-height: none; overflow: hidden; background: var(--ch-color-main-bg); color: var(--ch-color-text); box-shadow: 0 24px 80px rgb(0 0 0 / 35%); }
.attachment-preview-dialog::backdrop { background: rgb(0 0 0 / 65%); }
@media (max-width: 600px) {
  .attachment-preview-dialog { inset: 0; margin: 0; width: 100%; height: 100dvh; border: 0; border-radius: 0; }
}
</style>
