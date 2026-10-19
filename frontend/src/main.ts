import { createApp } from 'vue'
import { createPinia } from 'pinia'
import '@/features/app-shell/styles/theme.css'
import type { ClaudeHubNamespace } from '@/types'
import { rawHtmlPreviewPath } from '@/utils/markdownPreview'

// Create the single, application-wide namespace for any window-level globals
// (F8: consolidate stray `window.xxx =` assignments to prevent leaky reactive
// state from surviving unmount / cross-navigation).
if (typeof window !== 'undefined') {
  ;(window as Window).__claudeHub = {} as ClaudeHubNamespace
}

// Keep the standalone reader independent of the terminal/Workspace UI bundle.
const query = new URLSearchParams(window.location.search)
const rootComponent = query.has('markdown')
  ? import('@/views/MarkdownPreviewView.vue')
  : query.has('attachment') || rawHtmlPreviewPath(window.location.pathname)
    ? import('@/views/AttachmentPreviewView.vue') : import('./App.vue')

void rootComponent.then(({ default: root }) => {
  createApp(root).use(createPinia()).mount('#app')
}).catch(() => {
  const container = document.getElementById('app')
  if (container) container.textContent = 'Unable to load the page. Please refresh to try again.'
})
