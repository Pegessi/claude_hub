<template>
  <main class="attachment-reader">
    <AttachmentViewer
      v-if="preview.target.value"
      :target="preview.target.value"
      :can-go-back="preview.canGoBack.value"
      @back="preview.back"
    >
      <template #actions>
        <a
          class="ch-btn"
          :href="chatUrl"
        >Back to Hub</a>
      </template>
    </AttachmentViewer>
  </main>
</template>

<script setup lang="ts">
import '@/features/app-shell/styles/theme.css'
import { watch } from 'vue'
import AttachmentViewer from '@/components/AttachmentViewer.vue'
import { provideAttachmentPreview } from '@/composables/useAttachmentPreview'
import { useAppStore } from '@/stores/appStore'
import { rawHtmlPreviewPath } from '@/utils/markdownPreview'
const query = new URLSearchParams(window.location.search)
const scope = query.get('scope') || ''
const rawPath = rawHtmlPreviewPath(window.location.pathname)
const path = query.get('attachment') || rawPath
const registered = query.get('registered') === '1' || Boolean(rawPath)
const preview = provideAttachmentPreview()
preview.open({ scope, path, registered, hash: window.location.hash })
const chatUrl = !scope || scope.startsWith('/api/') ? '/' : `/?tab=${encodeURIComponent(scope)}`
const appStore = useAppStore()
watch(() => appStore.colorScheme, (theme) => { document.documentElement.dataset.theme = theme }, { immediate: true })
watch(preview.target, (target) => { document.title = `${target?.path.split('/').pop() || 'Attachment'} · Claude Hub` }, { immediate: true })
</script>

<style src="@/features/app-shell/styles/controls.css"></style>
<style scoped>
.attachment-reader { height: 100dvh; }
</style>
