// The hooks module (hooks/register.tsx) is type-checked only locally, against
// engine declarations CI does not have, so its imports never resolve on a CI
// runner. A src/ export that is renamed or dropped while register.tsx still
// imports it ships as "hooks module did not load" at session start (#307).
// This spec parses the hooks sources and their targets with the TypeScript
// compiler (already a dev dependency) and resolves every relative named
// import against the target module's real exports, so CI catches that drift
// before a release does. Parsing, not regex: exports that exist only inside
// comments or strings must not count.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import * as ts from 'typescript'

const mod = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..')
const HOOK_SOURCES = ['hooks/register.tsx', 'hooks/data.ts']

function parse(relpath: string, source: string): ts.SourceFile {
  const kind = relpath.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS
  return ts.createSourceFile(relpath, source, ts.ScriptTarget.ES2022, true, kind)
}

function hasModifier(node: ts.Node, kind: ts.SyntaxKind): boolean {
  return ts.canHaveModifiers(node) && (ts.getModifiers(node) ?? []).some(m => m.kind === kind)
}

function hasExportModifier(node: ts.Node): boolean {
  return hasModifier(node, ts.SyntaxKind.ExportKeyword)
}

// `export default function f()` exports a default, not a name: a named import
// of its local name must not resolve.
function hasDefaultModifier(node: ts.Node): boolean {
  return hasModifier(node, ts.SyntaxKind.DefaultKeyword)
}

function addBindingNames(name: ts.BindingName, out: Set<string>): void {
  if (ts.isIdentifier(name)) out.add(name.text)
  else for (const el of name.elements) if (ts.isBindingElement(el)) addBindingNames(el.name, out)
}

type Exports = { names: Set<string>; starFrom: string[] }

/** Every name a module exports: declarations, `export { ... }` lists and type lists. */
function exportedNames(relpath: string, source: string): Exports {
  const names = new Set<string>()
  const starFrom: string[] = []
  for (const st of parse(relpath, source).statements) {
    if (ts.isExportDeclaration(st)) {
      if (st.exportClause && ts.isNamedExports(st.exportClause)) {
        // `export { A, B as C, type T }` (with or without `from`): exports A, C, T.
        for (const el of st.exportClause.elements) names.add(el.name.text)
      } else if (!st.exportClause && st.moduleSpecifier && ts.isStringLiteral(st.moduleSpecifier)) {
        starFrom.push(st.moduleSpecifier.text)
      }
      continue
    }
    if (!hasExportModifier(st) || hasDefaultModifier(st)) continue
    if (ts.isVariableStatement(st)) {
      for (const d of st.declarationList.declarations) addBindingNames(d.name, names)
    } else if (
      (ts.isFunctionDeclaration(st) ||
        ts.isClassDeclaration(st) ||
        ts.isInterfaceDeclaration(st) ||
        ts.isTypeAliasDeclaration(st) ||
        ts.isEnumDeclaration(st) ||
        ts.isModuleDeclaration(st)) &&
      st.name
    ) {
      names.add(st.name.text)
    }
  }
  return { names, starFrom }
}

/** Every relative named import in a source: [name in the target module, specifier]. */
function namedImports(relpath: string, source: string): Array<[string, string]> {
  const out: Array<[string, string]> = []
  for (const st of parse(relpath, source).statements) {
    if (!ts.isImportDeclaration(st) || !ts.isStringLiteral(st.moduleSpecifier)) continue
    const specifier = st.moduleSpecifier.text
    if (!specifier.startsWith('.')) continue
    const bindings = st.importClause?.namedBindings
    if (bindings && ts.isNamedImports(bindings)) {
      for (const el of bindings.elements) out.push([(el.propertyName ?? el.name).text, specifier])
    }
    // Default and namespace imports are deliberately out of scope: the shipped
    // failure class is named-export drift, and the hooks sources use none.
  }
  return out
}

test('every relative named import in the hooks sources resolves to a real export', () => {
  const missing: string[] = []
  let scanned = 0
  for (const relpath of HOOK_SOURCES) {
    for (const [name, specifier] of namedImports(relpath, readFileSync(join(mod, relpath), 'utf8'))) {
      const target = resolve(mod, dirname(relpath), specifier)
      const exports = exportedNames(specifier, readFileSync(target, 'utf8'))
      if (!exports.names.has(name)) {
        const star =
          exports.starFrom.length > 0
            ? ` (it star-re-exports ${exports.starFrom.join(', ')}, which this guard does not resolve; extend the guard if the name arrives that way)`
            : ''
        missing.push(`${relpath}: '${specifier}' has no export named '${name}'${star}`)
      }
      scanned += 1
    }
  }
  // A scan that found nothing proves nothing: fail loudly if the import
  // grammar drifts past what this spec reads.
  assert.ok(scanned >= 20, `expected at least 20 relative named imports across the hooks sources, scanned ${scanned}`)
  assert.deepEqual(missing, [])
})

test('the export parser is not fooled by exports that exist only in comments or strings', () => {
  const source = [
    '// export const COMMENT_ONLY = 1',
    '/* export const BLOCK_COMMENT_ONLY = 2 */',
    'const s = `export const TEMPLATE_ONLY = 3`',
    "const t = 'export const STRING_ONLY = 4'",
    'export const REAL = 5',
  ].join('\n')
  const { names } = exportedNames('synthetic.ts', source)
  assert.deepEqual([...names], ['REAL'])
})

test('the export parser sees every declarator and every listed re-export', () => {
  const { names } = exportedNames('synthetic.ts', 'export const A = 1, B = 2\nexport { X } from \'./y.ts\'\nexport { P as Q, type T } from \'./z.ts\'')
  for (const n of ['A', 'B', 'X', 'Q', 'T']) assert.ok(names.has(n), `missing ${n}`)
  assert.equal(names.has('P'), false)
})

test('star re-exports are reported, not resolved or silently accepted', () => {
  const { names, starFrom } = exportedNames('synthetic.ts', "export * from './elsewhere.ts'\nexport const LOCAL = 1")
  assert.deepEqual(starFrom, ['./elsewhere.ts'])
  assert.deepEqual([...names], ['LOCAL'])
})

test('a default export is not collected as a named export', () => {
  const { names } = exportedNames('synthetic.ts', 'export default function main() {}\nexport const NAMED = 1')
  assert.deepEqual([...names], ['NAMED'])
})

test('the import parser reads multiline, type-only and aliased named imports', () => {
  const source = "import {\n  A,\n  type B,\n  C as D,\n} from './m.ts'\nimport side from './n.ts'\nimport * as ns from './o.ts'\nimport 'bare'"
  assert.deepEqual(namedImports('synthetic.ts', source), [
    ['A', './m.ts'],
    ['B', './m.ts'],
    ['C', './m.ts'],
  ])
})
