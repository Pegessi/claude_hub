import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import type { ClaudeHubNamespace } from '@/types'

// Create the single, application-wide namespace for any window-level globals
// (F8: consolidate stray `window.xxx =` assignments to prevent leaky reactive
// state from surviving unmount / cross-navigation).
if (typeof window !== 'undefined') {
  ;(window as Window).__claudeHub = {} as ClaudeHubNamespace
}

const app = createApp(App)
const pinia = createPinia()

app.use(pinia)
app.mount('#app')
