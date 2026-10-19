<template>
  <iframe
    ref="frame"
    class="attachment-html-preview"
    title="HTML preview"
    :sandbox="HTML_PREVIEW_SANDBOX"
    referrerpolicy="no-referrer"
    :srcdoc="preview.srcdoc"
    @load="initialize"
  />
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { HTML_PREVIEW_SANDBOX, htmlPreviewDocument } from '@/utils/htmlPreview'

const props = defineProps<{ content: string; hash?: string }>()
const emit = defineEmits<{ close: [] }>()
const frame = ref<HTMLIFrameElement | null>(null)
const preview = computed(() => htmlPreviewDocument(props.content))

function initialize() {
  frame.value?.contentWindow?.postMessage({
    type: 'attachment-html-init', channel: preview.value.channel, hash: props.hash,
  }, '*')
}
function onMessage(event: MessageEvent) {
  if (event.source !== frame.value?.contentWindow || event.origin !== 'null') return
  if (event.data?.type === 'attachment-html-close' && event.data.channel === preview.value.channel) emit('close')
}
watch(() => props.hash, initialize)
onMounted(() => window.addEventListener('message', onMessage))
onUnmounted(() => window.removeEventListener('message', onMessage))
</script>

<style scoped>
.attachment-html-preview { display: block; width: 100%; height: 100%; border: 0; background: white; }
</style>
