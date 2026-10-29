// Pure formatting helpers for the passive-proxy workbench.

export function formatSize(n) {
  const v = Number(n) || 0
  if (v >= 1048576) return `${(v / 1048576).toFixed(1)} MB`
  if (v >= 1024) return `${(v / 1024).toFixed(1)} KB`
  return `${v} B`
}

export function formatDuration(ms) {
  const value = Number(ms)
  if (!Number.isFinite(value) || value <= 0) return '—'
  if (value >= 1000) return `${(value / 1000).toFixed(value >= 10000 ? 0 : 1)} s`
  return `${Math.round(value)} ms`
}

export function formatClock(ts) {
  if (!ts) return ''
  const d = new Date(ts * 1000)
  const pad = (n, w = 2) => String(n).padStart(w, '0')
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${pad(d.getMilliseconds(), 3)}`
}

/** Status bucket used for row rails and status filter chips. */
export function statusBucket(flow) {
  if (flow?.error) return 'failed'
  const n = Number(flow?.status) || 0
  if (n >= 500) return '5xx'
  if (n >= 400) return '4xx'
  if (n >= 300) return '3xx'
  if (n >= 200) return '2xx'
  if (n >= 100) return '1xx'
  return 'failed'
}

export const STATUS_BUCKETS = ['2xx', '3xx', '4xx', '5xx', 'failed']

// ``resource_kind`` values from proxy_media.classify_flow.
export const RESOURCE_KINDS = ['api', 'document', 'script', 'stylesheet', 'image', 'video', 'other']

export const METHODS = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'HEAD']

export function flowPath(flow) {
  try {
    const parsed = new URL(flow?.url || '')
    return `${parsed.pathname || '/'}${parsed.search || ''}`
  } catch {
    return flow?.url || '—'
  }
}

export function formatHeaders(headers) {
  if (!headers || typeof headers !== 'object') return ''
  return Object.entries(headers).map(([k, v]) => `${k}: ${v}`).join('\n')
}

export function headerEntries(headers) {
  if (!headers || typeof headers !== 'object') return []
  return Object.entries(headers).map(([name, value]) => ({ name, value: String(value ?? '') }))
}

function shellQuote(value) {
  return `'${String(value).replace(/'/g, `'\\''`)}'`
}

// Recomputed by curl itself; copying them breaks replay of edited bodies.
const CURL_SKIP_HEADERS = new Set(['content-length', 'host', 'connection', 'proxy-connection', 'accept-encoding'])

/** POSIX-shell cURL command for a flow detail (``get_proxy_flow_detail``). */
export function toCurl(detail) {
  if (!detail?.url) return ''
  const method = String(detail.method || 'GET').toUpperCase()
  const parts = ['curl']
  if (method !== 'GET') parts.push('-X', method)
  parts.push(shellQuote(detail.url))
  for (const [name, value] of Object.entries(detail.request_headers || {})) {
    if (CURL_SKIP_HEADERS.has(name.toLowerCase())) continue
    parts.push('-H', shellQuote(`${name}: ${value}`))
  }
  const body = detail.request_body
  if (body && body.kind !== 'empty') {
    // Binary bodies are not inlined; the user saves the body and points curl at it.
    if (typeof body.text === 'string') parts.push('--data-raw', shellQuote(body.text))
    else parts.push('--data-binary', shellQuote('@request-body.bin'))
  }
  parts.push('--compressed')
  return parts.join(' ')
}

export function hexDump(hex) {
  if (!hex) return ''
  const lines = []
  for (let i = 0; i < hex.length; i += 32) {
    const bytes = hex.slice(i, i + 32).match(/.{1,2}/g) || []
    const offset = (i / 2).toString(16).padStart(8, '0')
    const ascii = bytes
      .map((h) => {
        const code = parseInt(h, 16)
        return code >= 32 && code < 127 ? String.fromCharCode(code) : '.'
      })
      .join('')
    lines.push(`${offset}  ${bytes.join(' ').padEnd(47, ' ')}  ${ascii}`)
  }
  return lines.join('\n')
}

export function base64ToBytes(b64) {
  try {
    const bin = atob(b64)
    const arr = new Uint8Array(bin.length)
    for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i)
    return arr
  } catch {
    return null
  }
}

/** Finding rule id → short i18n key under ``proxy.rules``. */
export function ruleKey(findingId) {
  return String(findingId || '').replace(/^proxy\./, '').replace(/-([a-z])/g, (_, c) => c.toUpperCase())
}

/** Titles embed counts ("Missing security headers (3)"); strip for group labels. */
export function ruleFallbackLabel(title) {
  return String(title || '').replace(/\s*\(\d+\)/g, '').trim()
}

const SEVERITY_ORDER = { critical: 0, high: 1, medium: 2, low: 3, info: 4 }

export function severityRank(sev) {
  return SEVERITY_ORDER[String(sev || 'info').toLowerCase()] ?? 5
}
