import { t } from '../../../i18n.js'

export function compactMeta(value) {
  if (value == null || value === '') return ''
  if (typeof value === 'string') return value
  try {
    return JSON.stringify(value)
  } catch (_) {
    return String(value)
  }
}

export function escapeHtml(text) {
  return String(text ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

export function formatPreviewContent(raw, mode) {
  const text = String(raw ?? '')
  if (mode !== 'json') return text
  const trimmed = text.trim()
  if (!trimmed) return text
  if (trimmed.includes('\n') && trimmed.split('\n').every(line => !line.trim() || line.trim().startsWith('{'))) {
    return text
  }
  try {
    return JSON.stringify(JSON.parse(text), null, 2)
  } catch (_) {
    return text
  }
}

export function highlightCode(raw, language) {
  const text = String(raw ?? '')
  const keywords = {
    python: 'and|as|assert|async|await|break|class|continue|def|del|elif|else|except|False|finally|for|from|global|if|import|in|is|lambda|None|nonlocal|not|or|pass|raise|return|True|try|while|with|yield',
    javascript: 'async|await|break|case|catch|class|const|continue|default|delete|else|export|false|finally|for|from|function|if|import|in|instanceof|let|new|null|return|switch|this|throw|true|try|typeof|undefined|var|while',
    json: 'true|false|null',
    shell: 'case|do|done|elif|else|fi|for|function|if|in|then|while',
  }
  const builtins = {
    python: 'bool|bytes|dict|float|int|list|object|set|str|tuple|Exception|Path',
    javascript: 'Array|Boolean|Date|Error|JSON|Map|Math|Number|Object|Promise|RegExp|Set|String|console|document|window',
    json: '',
    shell: '',
  }
  const keywordSource = keywords[language] || ''
  const builtinSource = builtins[language] || ''
  const tokenRe = new RegExp(
    [
      String.raw`("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|` + '`' + String.raw`(?:\\.|[^` + '`' + String.raw`\\])*` + '`' + String.raw`)`,
      String.raw`(\/\/[^\n]*|#[^\n]*|\/\*[\s\S]*?\*\/|<!--[\s\S]*?-->)`,
      String.raw`\b(def)\s+([A-Za-z_$][\w$]*)`,
      String.raw`\b(class)\s+([A-Za-z_$][\w$]*)`,
      builtinSource ? String.raw`\b(` + builtinSource + String.raw`)\b` : String.raw`(?!)`,
      keywordSource ? String.raw`\b(` + keywordSource + String.raw`)\b` : String.raw`(?!)`,
      String.raw`\b(-?\d+(?:\.\d+)?)\b`,
      String.raw`\b([A-Za-z_$][\w$]*)(?=\s*\()`,
    ].join('|'),
    'g',
  )
  let out = ''
  let last = 0
  text.replace((tokenRe), (match, str, comment, defKw, defName, classKw, className, builtin, keyword, number, fnName, index) => {
    out += escapeHtml(text.slice(last, index))
    if (defName || className) {
      const kw = defKw || classKw
      const name = defName || className
      const nameClass = defName ? 'function' : 'class'
      const sep = match.slice(kw.length, match.length - name.length)
      out += `<span class="syntax-keyword">${escapeHtml(kw)}</span>${escapeHtml(sep)}<span class="syntax-${nameClass}">${escapeHtml(name)}</span>`
    } else {
      const cls = comment
        ? 'comment'
        : str
          ? 'string'
          : builtin
            ? 'builtin'
            : keyword
              ? 'keyword'
              : number
                ? 'number'
                : fnName
                  ? 'function'
                  : ''
      out += cls ? `<span class="syntax-${cls}">${escapeHtml(match)}</span>` : escapeHtml(match)
    }
    last = index + match.length
    return match
  })
  out += escapeHtml(text.slice(last))
  return out
}

export function lineAndColumnFromIndex(text, index) {
  const safeIndex = Math.max(0, Math.min(index, String(text || '').length))
  const before = String(text || '').slice(0, safeIndex)
  const lines = before.split('\n')
  return {
    line: lines.length,
    column: (lines[lines.length - 1] || '').length + 1,
  }
}

export function kbActionLabel(action) {
  switch (action) {
    case 'saved': return t('kb.notify.saved')
    case 'updated': return t('kb.notify.updated')
    case 'superseded': return t('kb.notify.superseded')
    case 'deleted': return t('kb.notify.deleted')
    default: return t('kb.notify.changed')
  }
}

export function tableCellValue(row, column) {
  const key = column?.key
  const cells = row?.cells || row || {}
  const value = key ? cells[key] : ''
  if (Array.isArray(value)) return value.join(', ')
  if (value == null || value === '') return '—'
  return String(value)
}

export function resourceId(item) {
  return String(item?.id || item?.relative_path || item?.path || item?.name || '')
}

export function resourcePath(item) {
  return String(item?.relative_path || item?.path || '')
}

export function resourceExt(item) {
  const name = String(item?.name || resourcePath(item) || '')
  const m = name.toLowerCase().match(/\.([a-z0-9]+)$/)
  return m ? m[1] : ''
}

export function resourceTypeLabel(item) {
  if (item?.kind === 'dir') return 'DIR'
  const ext = resourceExt(item)
  return ext ? ext.toUpperCase().slice(0, 8) : 'FILE'
}

export function normalizeResourceItem(item) {
  const path = resourcePath(item)
  return {
    ...item,
    id: String(item?.id || path || item?.name || ''),
    name: String(item?.name || (path ? path.split('/').pop() : '') || ''),
    path,
    relative_path: path,
    kind: item?.kind === 'dir' ? 'dir' : 'file',
  }
}

export function formatBytes(value) {
  const n = Number(value || 0)
  if (!Number.isFinite(n) || n <= 0) return ''
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

export function compactList(items, limit = 6) {
  const arr = Array.isArray(items) ? items.filter(Boolean) : []
  const shown = arr.slice(0, limit)
  const more = arr.length > shown.length ? ` +${arr.length - shown.length}` : ''
  return shown.length ? shown.join(', ') + more : '—'
}

