// Every named value imported from a local module must be exported by it.
// hooks/register.tsx is not type-checked in CI (it needs the engine's
// generated types), so a removed export would otherwise surface only as a
// load-time SyntaxError inside the host.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync, existsSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const DIRS = ['hooks', 'src']

const IMPORT_RE = /import\s*\{([^}]*)\}\s*from\s*['"](\.{1,2}\/[^'"]+)['"]/g

export function namedValueImports(source: string): { names: string[]; spec: string }[] {
  const out: { names: string[]; spec: string }[] = []
  for (const m of source.matchAll(IMPORT_RE)) {
    const list = m[1] ?? ''
    const spec = m[2] ?? ''
    const names = list
      .split(',')
      .map((s) => s.trim())
      .filter((s) => s && !s.startsWith('type '))
      .map((s) => (s.split(/\s+as\s+/)[0] ?? '').trim())
    out.push({ names, spec })
  }
  return out
}

export function exportsOf(source: string): Set<string> {
  const names = new Set<string>()
  for (const m of source.matchAll(/export\s+(?:declare\s+)?(?:async\s+)?(?:const|let|var|function\*?|class|enum)\s+([A-Za-z_$][\w$]*)/g)) {
    if (m[1]) names.add(m[1])
  }
  for (const m of source.matchAll(/export\s*\{([^}]*)\}/g)) {
    for (const part of (m[1] ?? '').split(',')) {
      const name = part.trim().split(/\s+as\s+/).pop()?.trim()
      if (name && !name.startsWith('type ')) names.add(name)
    }
  }
  return names
}

export function missingImports(files: Map<string, string>): string[] {
  const missing: string[] = []
  for (const [file, source] of files) {
    for (const { names, spec } of namedValueImports(source)) {
      const target = resolve(dirname(file), spec)
      const targetSource = files.get(target) ?? (existsSync(target) ? readFileSync(target, 'utf8') : null)
      if (targetSource === null) continue
      const exported = exportsOf(targetSource)
      for (const name of names) {
        if (!exported.has(name)) missing.push(`${file}: '${name}' is not exported by ${spec}`)
      }
    }
  }
  return missing
}

function sourceFiles(): Map<string, string> {
  const files = new Map<string, string>()
  const walk = (dir: string) => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const path = join(dir, entry.name)
      if (entry.isDirectory()) walk(path)
      else if (/\.tsx?$/.test(entry.name)) files.set(path, readFileSync(path, 'utf8'))
    }
  }
  for (const d of DIRS) walk(join(ROOT, d))
  return files
}

test('every named value imported from a local module is exported by it', () => {
  assert.deepEqual(missingImports(sourceFiles()), [])
})

test('a removed export is reported (known-bad)', () => {
  const files = new Map([
    ['/x/hooks/a.tsx', "import { GONE, KEPT, type T } from '../src/b.ts'\n"],
    ['/x/src/b.ts', 'export const KEPT = 1\nexport type T = number\n'],
  ])
  assert.deepEqual(missingImports(files), ["/x/hooks/a.tsx: 'GONE' is not exported by ../src/b.ts"])
})
