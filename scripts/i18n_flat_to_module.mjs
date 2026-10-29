import { readFileSync, writeFileSync } from 'fs'

function unflatten(flat) {
  const tree = {}
  const paths = Object.keys(flat).sort((a, b) => a.split('.').length - b.split('.').length || a.localeCompare(b))
  for (const path of paths) {
    const parts = String(path).split('.')
    let cur = tree
    let consumed = false
    for (let i = 0; i < parts.length - 1; i++) {
      if (typeof cur[parts[i]] === 'string') {
        cur[parts.slice(i).join('.')] = flat[path]
        consumed = true
        break
      }
      if (!cur[parts[i]] || typeof cur[parts[i]] !== 'object' || Array.isArray(cur[parts[i]])) {
        cur[parts[i]] = {}
      }
      cur = cur[parts[i]]
    }
    if (!consumed) cur[parts[parts.length - 1]] = flat[path]
  }
  return tree
}

const id = process.argv[2]
if (!id) {
  console.error('usage: node scripts/i18n_flat_to_module.mjs <locale-id>')
  process.exit(1)
}
const en = JSON.parse(readFileSync('argusai/webui/src/i18n/locales/_en.flat.json', 'utf8'))
const overlay = JSON.parse(readFileSync(`argusai/webui/src/i18n/locales/_${id}.flat.json`, 'utf8'))
const base = id === 'zh-Hant'
  ? JSON.parse(readFileSync('argusai/webui/src/i18n/locales/_zh.flat.json', 'utf8'))
  : en
const flat = { ...base, ...overlay }
const missing = Object.keys(base).filter((k) => !(k in overlay)).length
writeFileSync(
  `argusai/webui/src/i18n/locales/${id}.js`,
  `export default ${JSON.stringify(unflatten(flat), null, 2)}\n`,
)
console.log(id, 'overlay', Object.keys(overlay).length, 'missing', missing)
