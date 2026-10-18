import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const source = path => readFile(new URL(`../src/${path}`, import.meta.url), 'utf8')

const componentFacades = [
  ['components/ScheduledTasksPanel.vue', 'features/scheduled-tasks/ScheduledTasksPanel.vue'],
  ['components/DotPanel.vue', 'features/dot/DotPanel.vue'],
  ['components/AgentStatusFloatingPanel.vue', 'features/agent-status/AgentStatusFloatingPanel.vue'],
  ['components/FeishuBotSettingsDialog.vue', 'features/feishu-bot/FeishuBotSettingsDialog.vue'],
]

test('public panel modules default-export their feature component', async () => {
  for (const [facadePath, implementationPath] of componentFacades) {
    const facade = await source(facadePath)
    const componentName = implementationPath.split('/').at(-1).replace('.vue', '')
    assert.match(facade, new RegExp(`import ${componentName} from ['"]@/${implementationPath}['"]`))
    assert.match(facade, new RegExp(`export default ${componentName}`))
  }
})

test('public store modules re-export the only Pinia store definition', async () => {
  const terminalFacade = await source('stores/terminalStore.ts')
  const terminalImplementation = await source('stores/terminal/index.ts')
  assert.match(terminalFacade, /export \{ useTerminalStore \} from ['"]\.\/terminal\/index['"]/)
  assert.equal((terminalFacade + terminalImplementation).match(/defineStore\('terminal'/g)?.length, 1)

  const workspaceFacade = await source('stores/workspaceStore.ts')
  const workspaceImplementation = await source('stores/workspace/index.ts')
  assert.match(workspaceFacade, /export \{ useWorkspaceStore \} from ['"]\.\/workspace\/index['"]/)
  assert.equal((workspaceFacade + workspaceImplementation).match(/defineStore\('workspace'/g)?.length, 1)
})
