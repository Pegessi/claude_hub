import { Buffer } from 'node:buffer'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'

const vueUrl = await import.meta.resolve('vue')
const piniaUrl = await import.meta.resolve('pinia')
const source = path => readFile(new URL(`../src/${path}`, import.meta.url), 'utf8')

function moduleUrl(text, replacements = {}) {
  let output = ts.transpileModule(text, {
    compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2020 },
  }).outputText
  for (const [specifier, url] of Object.entries(replacements)) {
    output = output.replaceAll(`from '${specifier}'`, `from ${JSON.stringify(url)}`)
  }
  return `data:text/javascript;base64,${Buffer.from(output).toString('base64')}`
}

const chatGroupsUrl = moduleUrl(await source('utils/chatGroups.ts'))
const reconnectUrl = moduleUrl(await source('utils/terminalReconnect.ts'), { vue: vueUrl })
const helpersUrl = moduleUrl(await source('stores/terminal/helpers.ts'))
const notificationsUrl = moduleUrl(await source('stores/notifications.ts'), { vue: vueUrl })
const mobileDrawerUrl = moduleUrl(await source('stores/terminal/mobileDrawer.ts'), { vue: vueUrl })
const queuedChatUrl = moduleUrl(await source('stores/terminal/queuedChatRuntime.ts'), { vue: vueUrl })
const sidebarUrl = moduleUrl(await source('stores/terminal/sidebarPreferences.ts'), {
  '@/utils/chatGroups': chatGroupsUrl,
  './helpers': helpersUrl,
})
const statusUrl = moduleUrl(await source('stores/terminal/agentStatusPolling.ts'), {
  './helpers': helpersUrl,
})
const paneReconnectUrl = moduleUrl(await source('stores/terminal/paneReconnect.ts'), {
  '@/utils/terminalReconnect': reconnectUrl,
})
const storeUrl = moduleUrl(await source('stores/terminal/index.ts'), {
  pinia: piniaUrl,
  vue: vueUrl,
  '@/utils/chatGroups': chatGroupsUrl,
  './helpers': helpersUrl,
  './agentStatusPolling': statusUrl,
  './mobileDrawer': mobileDrawerUrl,
  './paneReconnect': paneReconnectUrl,
  './queuedChatRuntime': queuedChatUrl,
  './sidebarPreferences': sidebarUrl,
  '../notifications': notificationsUrl,
})

export const chatGroupsModuleUrl = chatGroupsUrl
export const { useTerminalStore } = await import(storeUrl)
