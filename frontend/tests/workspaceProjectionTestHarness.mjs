import { Buffer } from 'node:buffer'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'

const moduleUrls = new Map()

export async function workspaceProjectionModuleUrl(name) {
  if (moduleUrls.has(name)) return moduleUrls.get(name)
  const source = await readFile(new URL(`../src/utils/${name}.ts`, import.meta.url), 'utf8')
  let { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 },
  })
  if (outputText.includes('@/utils/duration')) {
    outputText = outputText.replace('@/utils/duration', await workspaceProjectionModuleUrl('duration'))
  }
  const url = `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
  moduleUrls.set(name, url)
  return url
}

export async function loadWorkspaceProjection(name) {
  return import(await workspaceProjectionModuleUrl(name))
}
