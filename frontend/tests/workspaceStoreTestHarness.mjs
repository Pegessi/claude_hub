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

const paginationUrl = moduleUrl(await source('utils/boardPagination.ts'))
const notificationsUrl = moduleUrl(await source('stores/notifications.ts'), { vue: vueUrl })
const actionsUrl = moduleUrl(await source('stores/workspace/actions.ts'))
const storeUrl = moduleUrl(await source('stores/workspace/index.ts'), {
  pinia: piniaUrl,
  vue: vueUrl,
  '@/utils/boardPagination': paginationUrl,
  '../notifications': notificationsUrl,
  './actions': actionsUrl,
})

export const { useWorkspaceStore } = await import(storeUrl)
