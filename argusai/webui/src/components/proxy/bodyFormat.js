// Body formatting for the proxy inspector.
//
// JSON is re-indented by a tokenizer instead of JSON.parse/stringify so number
// literals stay byte-for-byte (64-bit ids, 0.10, 1e400) and key order and
// duplicate keys survive — what the server actually sent is the evidence.

// Above this the formatted text is shown without per-token <span>s.
export const HIGHLIGHT_LIMIT = 300 * 1024

// Anti-JSON-hijacking prefixes some APIs put before the payload.
const XSSI_PREFIXES = [")]}',", ")]}'", 'for(;;);', 'while(1);']

const WS = new Set([0x20, 0x0a, 0x0d, 0x09])
const DELIM = new Set([0x20, 0x0a, 0x0d, 0x09, 0x2c, 0x3a, 0x5b, 0x5d, 0x7b, 0x7d]) // ws , : [ ] { }
const LITERAL_RE = /^(?:-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false|null)$/

export function escapeHtml(text) {
  return String(text)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

function stripXssiPrefix(text) {
  const trimmed = text.replace(/^\uFEFF/, '').trimStart()
  for (const prefix of XSSI_PREFIXES) {
    if (trimmed.startsWith(prefix)) return { body: trimmed.slice(prefix.length).trimStart(), prefix }
  }
  return { body: trimmed, prefix: '' }
}

export function looksLikeJson(text) {
  const { body } = stripXssiPrefix(String(text || '').slice(0, 64))
  const first = body.trimStart()[0]
  return first === '{' || first === '['
}

/**
 * Pretty-print JSON text. Returns ``{ ok, partial, text, html, prefix }``;
 * ``html`` is null when the output is too large to highlight. A body that is
 * valid so far but cut off (capture cap, transport limit) formats as far as it
 * goes with ``partial: true``; anything malformed returns ``ok: false``.
 */
export function formatJson(source) {
  const { body: src, prefix } = stripXssiPrefix(String(source || ''))
  const n = src.length
  if (!n || (src[0] !== '{' && src[0] !== '[')) return { ok: false }
  const plain = []
  const tokens = []
  const stack = []
  let indent = 0
  let expectKey = false
  let i = 0

  const push = (value, cls) => {
    plain.push(value)
    tokens.push(cls ? [value, cls] : [value, ''])
  }
  const newline = () => push(`\n${'  '.repeat(indent)}`)
  let partial = false

  while (i < n) {
    const code = src.charCodeAt(i)
    if (WS.has(code)) {
      i += 1
      continue
    }
    const ch = src[i]
    if (ch === '{' || ch === '[') {
      const close = ch === '{' ? '}' : ']'
      let j = i + 1
      while (j < n && WS.has(src.charCodeAt(j))) j += 1
      if (src[j] === close) {
        push(ch + close, 'p')
        i = j + 1
        continue
      }
      push(ch, 'p')
      stack.push(ch)
      indent += 1
      newline()
      expectKey = ch === '{'
      i += 1
      continue
    }
    if (ch === '}' || ch === ']') {
      const open = stack.pop()
      if (!open || (open === '{') !== (ch === '}')) return { ok: false }
      indent -= 1
      newline()
      push(ch, 'p')
      i += 1
      if (!stack.length) {
        if (src.slice(i).trim()) return { ok: false }
        break
      }
      continue
    }
    if (ch === ',') {
      if (!stack.length) return { ok: false }
      push(',', 'p')
      newline()
      expectKey = stack[stack.length - 1] === '{'
      i += 1
      continue
    }
    if (ch === ':') {
      push(': ', 'p')
      expectKey = false
      i += 1
      continue
    }
    if (ch === '"') {
      let j = i + 1
      while (j < n) {
        const c = src.charCodeAt(j)
        if (c === 0x5c) j += 2
        else if (c === 0x22) break
        else j += 1
      }
      if (j >= n) {
        // String cut off by the end of the captured text.
        push(src.slice(i), expectKey ? 'k' : 's')
        partial = true
        break
      }
      push(src.slice(i, j + 1), expectKey ? 'k' : 's')
      i = j + 1
      continue
    }
    let j = i
    while (j < n && !DELIM.has(src.charCodeAt(j))) j += 1
    const literal = src.slice(i, j)
    if (!LITERAL_RE.test(literal)) {
      // A literal cut at the very end ("tr", "12e") is truncation, not an error.
      if (j < n || !/^-?[\d.eE+-]*$|^t(r(u)?)?$|^f(a(l(s)?)?)?$|^n(u(l)?)?$/.test(literal)) return { ok: false }
      push(literal)
      partial = true
      break
    }
    push(literal, literal[0] === 't' || literal[0] === 'f' || literal[0] === 'n' ? 'l' : 'n')
    i = j
  }
  if (stack.length) partial = true
  const text = plain.join('')
  let html = null
  if (text.length <= HIGHLIGHT_LIMIT) {
    html = tokens
      .map(([value, cls]) => (cls ? `<span class="j-${cls}">${escapeHtml(value)}</span>` : escapeHtml(value)))
      .join('')
  }
  return { ok: true, partial, text, html, prefix }
}

/** ``a=1&b=%E4%BD%A0`` → ``[['a','1'], ['b','你']]``; null when it is not a form body. */
export function parseFormBody(text, contentType = '') {
  const value = String(text || '').trim()
  if (!value || value.length > 512 * 1024) return null
  const declared = String(contentType).toLowerCase().includes('application/x-www-form-urlencoded')
  if (!declared && !/^[\w.\-~%[\]]+=[^&\s]*(?:&[\w.\-~%[\]]+=[^&\s]*)*$/.test(value)) return null
  try {
    const rows = []
    for (const [k, v] of new URLSearchParams(value)) rows.push([k, v])
    return rows.length ? rows : null
  } catch {
    return null
  }
}
