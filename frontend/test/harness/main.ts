import { createApp } from 'vue'
import { createPinia } from 'pinia'
import TerminalHmrHarnessApp from './TerminalHmrHarnessApp.vue'
import type { ClaudeHubNamespace } from '@/types'

if (typeof window !== 'undefined') {
  window.__claudeHub = {} as ClaudeHubNamespace
}

const app = createApp(TerminalHmrHarnessApp)
app.use(createPinia())
app.mount('#app')
