import { Buffer } from 'node:buffer'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'

// Preserve module boundaries so tests execute the utilities' real dependencies,
// including relative imports, rather than concatenating extracted source bodies.
const moduleUrls = new Map()
async function moduleUrl(file) {
  if (moduleUrls.has(file.href)) return moduleUrls.get(file.href)
  const source = await readFile(file, 'utf8')
  let { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2020 },
  })
  for (const [, specifier] of outputText.matchAll(/from ['"]([^'"]+)['"]/g)) {
    const dependency = specifier.startsWith('@/')
      ? new URL(`../src/${specifier.slice(2)}.ts`, import.meta.url)
      : specifier.startsWith('.') ? new URL(`${specifier}.ts`, file) : null
    const url = dependency ? await moduleUrl(dependency) : import.meta.resolve(specifier)
    outputText = outputText
      .replaceAll(`'${specifier}'`, JSON.stringify(url))
      .replaceAll(`"${specifier}"`, JSON.stringify(url))
  }
  const url = `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
  moduleUrls.set(file.href, url)
  return url
}

export async function loadChatUtility(name) {
  return import(await moduleUrl(new URL(`../src/utils/${name}.ts`, import.meta.url)))
}

export async function loadChatFeature(path) {
  return import(await moduleUrl(new URL(`../src/features/chat/${path}.ts`, import.meta.url)))
}
