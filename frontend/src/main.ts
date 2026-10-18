import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import MarkdownPreviewView from '@/views/MarkdownPreviewView.vue'
import type { ClaudeHubNamespace } from '@/types'

// Create the single, application-wide namespace for any window-level globals
// (F8: consolidate stray `window.xxx =` assignments to prevent leaky reactive
// state from surviving unmount / cross-navigation).
if (typeof window !== 'undefined') {
  ;(window as Window).__claudeHub = {} as ClaudeHubNamespace
}

// Use the existing SPA root so deep links also work after a production reload.
const isMarkdownPreview = new URLSearchParams(window.location.search).has('markdown')
const app = createApp(isMarkdownPreview ? MarkdownPreviewView : App)
const pinia = createPinia()

app.use(pinia)
app.mount('#app')
