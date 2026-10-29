<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { api } from '../../api.js'
import { i18n, t } from '../../i18n.js'
import { useMediaPreview } from '../../composables/useMediaPreview.js'
import MindmapView from './MindmapView.vue'
import { isMindmapSource, parseMindmap } from './mindmapParse.js'
import { copyTableAsImage } from './tableCopyImage.js'

const props = defineProps({
  text: { type: String, default: '' },
  sessionId: { type: String, default: '' },
})
const mediaPreview = useMediaPreview()

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

const KNOWN_PATH_PREFIXES = [
  'argusai', 'scripts', 'tests', 'docs', 'src',
  'webui', 'core', 'skills', 'vendor', 'dist',
  'data_root', '.cursor', '.claude', '.venv', 'node_modules',
]

const KNOWN_FILE_EXTS = new Set([
  'md', 'py', 'vue', 'js', 'mjs', 'cjs', 'ts', 'tsx', 'jsx',
  'json', 'yml', 'yaml', 'toml', 'ini', 'cfg',
  'txt', 'log', 'csv',
  'html', 'htm', 'css', 'scss',
  'png', 'jpg', 'jpeg', 'gif', 'svg', 'webp',
  'pdf', 'zip', 'tar', 'gz', '7z',
  'wasm', 'wat', 'map',
  'sh', 'ps1', 'bat', 'cmd',
  'proto', 'sql', 'xml',
])

function hasKnownExt(s) {
  const dot = s.lastIndexOf('.')
  if (dot <= 0) return false
  if (dot < s.lastIndexOf('/')) return false
  if (dot < s.lastIndexOf('\\')) return false
  const ext = s.slice(dot + 1).toLowerCase().replace(/[^a-z0-9]+$/, '')
  return Boolean(ext) && KNOWN_FILE_EXTS.has(ext)
}

function isPreviewableMediaPath(raw) {
  const s = String(raw || '').split(/[?#]/, 1)[0].toLowerCase()
  const dot = s.lastIndexOf('.')
  if (dot <= 0) return false
  const ext = s.slice(dot + 1).replace(/[^a-z0-9]+$/, '')
  return [
    'png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp',
    'pdf',
    'mp4', 'webm', 'mov', 'm4v', 'avi', 'mkv',
    'mp3', 'wav', 'ogg', 'm4a', 'flac', 'aac',
    'txt', 'log', 'md', 'json', 'jsonl', 'csv', 'yaml', 'yml',
    'toml', 'ini', 'py', 'js', 'ts', 'html', 'css', 'xml', 'sql',
  ].includes(ext)
}

function looksLikePath(raw) {
  if (!raw) return false
  const s = String(raw).trim()
  if (s.length < 3 || s.length > 400) return false
  if (/\s{2,}/.test(s)) return false
  if (/^https?:\/\//i.test(s)) return false
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(s)) return false

  if (/^[A-Za-z]:[\\/]/.test(s)) return true
  if (s.startsWith('/') && s.length > 1 && /\/[^\s/]+/.test(s.slice(1))) return true

  if (/[\\/]/.test(s)) {
    const head = s.split(/[\\/]/, 1)[0].toLowerCase()
    if (KNOWN_PATH_PREFIXES.includes(head)) return true
    if (hasKnownExt(s)) return true
    return false
  }

  if (hasKnownExt(s) && /[A-Za-z0-9_\u4e00-\u9fff]/.test(s)) {
    if (s === 'index.js' || s === 'package.json' || s === 'README.md') {
      return false
    }
    return true
  }
  return false
}

function trimTrailingPunct(s) {
  return s.replace(/[,;:.!?)\]}>'"`]+$/, '')
}

function pathDisplayName(raw) {
  const s = String(raw || '').trim().replace(/[\\/]+$/, '')
  if (!s) return ''
  const parts = s.split(/[\\/]/).filter(Boolean)
  return parts[parts.length - 1] || s
}

function makePathCandidate(raw, isInsideCode) {
  const trimmed = trimTrailingPunct(raw)
  const trailing = raw.slice(trimmed.length)
  const safe = escapeHtml(trimmed)
  const cls = isInsideCode ? 'path-candidate in-code' : 'path-candidate'
  const tail = trailing ? escapeHtml(trailing) : ''
  return `<span class="${cls}" data-path="${safe}" data-state="probing">${safe}</span>${tail}`
}

const PATH_CHAR = "A-Za-z0-9_.\\-\\u4e00-\\u9fff"
const PATH_TOKEN_RE = new RegExp(
  '(?:' +
    // Windows absolute: C:\foo\bar
    `[A-Za-z]:[\\\\/][^\\s\`<>"']+` +
    '|' +
    // POSIX absolute with at least two segments
    `/[^\\s\`<>"']+/[^\\s\`<>"']+` +
    '|' +
    // Relative with separators: foo/bar, foo\bar\baz
    `(?:[${PATH_CHAR}]+[\\\\/]){1,}[${PATH_CHAR}]+` +
    '|' +
    // Bare filename with a dot extension (e.g. report.md, 数据.json)
    `[${PATH_CHAR}]+\\.[A-Za-z0-9]{1,8}` +
    ')',
  'g',
)

function transformPaths(escapedSegment, isInsideCode) {
  return escapedSegment.replace(PATH_TOKEN_RE, (m) => {
    const cleaned = trimTrailingPunct(m)
    if (!looksLikePath(cleaned)) return m
    if (cleaned.length !== m.length) {
      return makePathCandidate(m, isInsideCode)
    }
    return makePathCandidate(cleaned, isInsideCode)
  })
}

function splitAndTransform(escaped, isInsideCode) {
  const placeholders = []
  const stashed = escaped.replace(/<span class="path-candidate[^>]*>[^<]*<\/span>/g, (m) => {
    placeholders.push(m)
    return `\u0000CHIP${placeholders.length - 1}\u0000`
  })
  const transformed = transformPaths(stashed, isInsideCode)
  return transformed.replace(/\u0000CHIP(\d+)\u0000/g, (_m, idx) => placeholders[Number(idx)] || '')
}

/**
 * Allowlist URL schemes for images embedded in chat output.
 *
 * Inline images come from AI/MCP responses, which we treat as untrusted.
 * Only HTTPS, data:image/*, and HTTP to loopback are allowed. ``javascript:``,
 * ``file://``, generic ``data:`` (which could be HTML/SVG with scripts),
 * and remote plain HTTP are rejected.
 */
function sanitizeImgUrl(rawUrl) {
  const url = String(rawUrl || '').trim()
  if (!url || url.length > 4096) return ''
  if (/[\u0000\r\n]/.test(url)) return ''
  const lower = url.toLowerCase()
  if (lower.startsWith('javascript:') || lower.startsWith('vbscript:') || lower.startsWith('file:')) return ''
  if (lower.startsWith('data:')) {
    return /^data:image\/(png|jpe?g|gif|webp|svg\+xml);/i.test(url) ? url : ''
  }
  if (lower.startsWith('https://')) return url
  if (lower.startsWith('http://')) {
    if (/^http:\/\/(localhost|127\.0\.0\.1|\[::1\])(:\d+)?\b/i.test(url)) return url
    // Allow remote plain HTTP too: the user already trusted the MCP server
    // that produced this URL, and our pywebview shell isn't shipping cookies.
    // We mitigate via referrerpolicy=no-referrer on the rendered <img>.
    return url
  }
  return ''
}

function unwrapMdUrl(raw) {
  let url = String(raw || '').trim()
  if (url.startsWith('&lt;') && url.endsWith('&gt;')) url = url.slice(4, -4).trim()
  if (url.startsWith('<') && url.endsWith('>')) url = url.slice(1, -1).trim()
  return url
}

function isLocalImageRef(url) {
  const s = String(url || '').trim()
  if (!s) return false
  if (/^(https?:|data:|javascript:|vbscript:|file:)/i.test(s)) return false
  const clean = s.split(/[?#]/, 1)[0].replace(/\\/g, '/')
  if (/\.(png|jpe?g|gif|webp|svg|bmp)$/i.test(clean)) return true
  if (/(?:^|\/)_(?:images|attachments|video|audio)\//i.test(clean)) return true
  return looksLikePath(s)
}

function renderMdImage(altRaw, urlRaw) {
  const alt = decodeAttr(String(altRaw || '').trim())
  const url = decodeAttr(unwrapMdUrl(urlRaw))
  const safe = sanitizeImgUrl(url)
  const altAttr = escapeHtml(alt)
  if (safe) {
    return `<img class="md-image" src="${escapeHtml(safe)}" alt="${altAttr}" loading="lazy" referrerpolicy="no-referrer">`
  }
  if (isLocalImageRef(url)) {
    return `<img class="md-image md-image-pending" data-path="${escapeHtml(url)}" alt="${altAttr}" src="" loading="lazy">`
  }
  return `<span class="md-image-ph" role="img" aria-label="${altAttr || 'image'}"></span>`
}

/**
 * Allowlist URL schemes for inline links. Stricter than images since links
 * are navigations, not just resource loads.
 */
function sanitizeLinkUrl(rawUrl) {
  const url = String(rawUrl || '').trim()
  if (!url || url.length > 4096) return ''
  if (/[\u0000\r\n]/.test(url)) return ''
  const lower = url.toLowerCase()
  if (lower.startsWith('https://') || lower.startsWith('http://') || lower.startsWith('mailto:')) return url
  return ''
}

/**
 * Peel sentence-ending punctuation off a bare URL match.
 * Keep balanced ()/[]/{} that are part of the URL (Wikipedia-style).
 */
function peelUrlTrailing(raw) {
  let url = String(raw || '')
  let trailing = ''
  while (url.length > 10) {
    const ch = url[url.length - 1]
    // Include markdown emphasis markers — `**https://x.com**` must not keep `*`.
    if (/[.,;:!?'"*»」』】）]/.test(ch)) {
      trailing = ch + trailing
      url = url.slice(0, -1)
      continue
    }
    if (ch === ')' || ch === ']' || ch === '}') {
      const open = ch === ')' ? '(' : ch === ']' ? '[' : '{'
      const body = url.slice(0, -1)
      const opens = (body.match(new RegExp(`\\${open}`, 'g')) || []).length
      const closes = (url.match(new RegExp(`\\${ch}`, 'g')) || []).length
      if (closes > opens) {
        trailing = ch + trailing
        url = body
        continue
      }
    }
    break
  }
  return { url, trailing }
}

// Stop at whitespace, quotes, markdown `*`/`_`, and CJK brackets so
// `**https://github.com**(说明)` never swallows emphasis or prose.
const BARE_URL_RE = /https?:\/\/[^\s<>"'`*_（）【】「」『』]+/gi

/** Autolink bare http(s) URLs in already-escaped prose (skip HTML tags / code). */
function autolinkBareUrls(escapedHtml, pushStash) {
  // Inline `code` is already wrapped; keep URLs inside it plain.
  const protectedChunks = []
  const mask = (m) => {
    protectedChunks.push(m)
    return `\u0002CODE${protectedChunks.length - 1}\u0002`
  }
  const masked = escapedHtml
    .replace(/<code>[\s\S]*?<\/code>/g, mask)
    .replace(/<span class="path-candidate[^>]*>[\s\S]*?<\/span>/g, mask)

  const linked = masked.replace(/(<[^>]+>)|([^<]+)/g, (_whole, tag, text) => {
    if (tag) return tag
    return text.replace(BARE_URL_RE, (raw) => {
      const { url, trailing } = peelUrlTrailing(raw)
      const safe = sanitizeLinkUrl(url)
      if (!safe) return raw
      return pushStash(
        `<a class="md-link" data-href="${safe}" href="${safe}" rel="noopener noreferrer">${url}</a>`,
      ) + trailing
    })
  })
  return linked.replace(/\u0002CODE(\d+)\u0002/g, (_m, idx) => protectedChunks[Number(idx)] || '')
}

function inlineMd(s) {
  const escaped = escapeHtml(s)

  // Stash markdown images and links BEFORE the path detector runs.
  // URLs contain dots and slashes that the path-token regex would
  // happily mis-classify as "looks like a path" (e.g.
  // ``mdn.alipayobjects.com/one_clip/...``). By pulling them out
  // up front and restoring at the very end, the path layer only
  // ever sees real prose.
  const stash = []
  const pushStash = (html) => {
    stash.push(html)
    return `\u0001MDX${stash.length - 1}\u0001`
  }
  const withMedia = escaped
    // ![alt](url) — must run before the plain link regex below, otherwise
    // the link regex would gobble the URL and we'd lose the image.
    .replace(/!\[([^\]\n]*)\]\((<[^>\n]+>|[^)\s]+)\)/g, (_m, altRaw, urlRaw) => {
      return pushStash(renderMdImage(altRaw, urlRaw))
    })
    // [text](url)
    .replace(/\[([^\]\n]+)\]\(([^)\s]+)\)/g, (_m, textRaw, urlRaw) => {
      const safe = sanitizeLinkUrl(decodeAttr(urlRaw))
      const text = String(textRaw || '').trim()
      if (!safe) return text
      return pushStash(
        `<a class="md-link" data-href="${escapeHtml(safe)}" href="${escapeHtml(safe)}" rel="noopener noreferrer">${text}</a>`
      )
    })

  const withCode = withMedia.replace(/`([^`]+)`/g, (_m, inner) => {
    const transformed = transformPaths(inner, true)
    if (transformed !== inner) return transformed
    return `<code>${inner}</code>`
  })
  // Emphasis BEFORE bare autolink so `**https://github.com**` becomes
  // <strong>…</strong> first; then the URL inside is linkified cleanly.
  const withEmphasis = withCode
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/\*([^*]+)\*/g, '<em>$1</em>')
  // Channel / model leftovers such as `[image] 这是什么` must not stay as
  // raw brackets after a conversation reload.
  const withPlaceholders = withEmphasis.replace(
    /(^|[^!])\[(?:image|img|media)\]/gi,
    (_m, pre) => pre + pushStash('<span class="md-image-ph" role="img" aria-label="image"></span>'),
  )
  // Bare https?://… after inline code so `code` stays plain; stash before
  // path detection so hosts like example.com/a/b.py aren't chipified.
  const withAutolink = autolinkBareUrls(withPlaceholders, pushStash)
  const result = splitAndTransform(withAutolink, false)

  // Restore the image/link placeholders verbatim — they're already HTML
  // emitted by us, not user content, so no further escaping needed.
  return result.replace(/\u0001MDX(\d+)\u0001/g, (_m, idx) => stash[Number(idx)] || '')
}

function isTableSep(line) {
  return /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(line)
}

const EXPAND_ICON_SVG = '<svg class="md-expand-icon" viewBox="0 0 16 16" width="13" height="13" aria-hidden="true" focusable="false"><path d="M9.25 2.5h4.25V6.75M6.75 13.5H2.5V9.25M13.5 2.5l-4.2 4.2M2.5 13.5l4.2-4.2" fill="none" stroke="currentColor" stroke-width="1.35" stroke-linecap="round" stroke-linejoin="round"/></svg>'
const COPY_IMAGE_ICON_SVG = '<svg class="md-copy-image-icon" viewBox="0 0 16 16" width="13" height="13" aria-hidden="true" focusable="false"><rect x="2.25" y="3.25" width="11.5" height="9.5" rx="1.6" fill="none" stroke="currentColor" stroke-width="1.35"/><circle cx="5.6" cy="6.35" r="1.05" fill="currentColor"/><path d="M3.4 11.15l3.05-3.05 1.85 1.85 2.15-2.45 2.15 3.65" fill="none" stroke="currentColor" stroke-width="1.35" stroke-linecap="round" stroke-linejoin="round"/></svg>'
const CHECK_ICON_SVG = '<svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true" focusable="false"><path d="M3.4 8.3l3.05 3.05 6.15-6.3" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>'

function renderExpandBtn(kind) {
  const label = escapeHtml(t('assistant.expandView'))
  return `<button type="button" class="md-table-btn md-expand-btn" data-expand="${kind}" title="${label}" aria-label="${label}">${EXPAND_ICON_SVG}</button>`
}

function renderCopyImageBtn() {
  const label = escapeHtml(t('assistant.copyTableImage'))
  return `<button type="button" class="md-table-btn md-copy-image-btn" data-copy-table-image title="${label}" aria-label="${label}">${COPY_IMAGE_ICON_SVG}</button>`
}

function renderTable(lines, start) {
  const header = lines[start]
  const sep = lines[start + 1]
  if (!header?.includes('|') || !isTableSep(sep || '')) return null
  const rows = []
  let i = start
  while (i < lines.length && lines[i].includes('|') && lines[i].trim()) {
    rows.push(lines[i])
    i += 1
  }
  const cells = row => row.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(c => inlineMd(c.trim()))
  const head = cells(rows[0])
  const body = rows.slice(2).map(cells)
  return {
    html: `<div class="md-table-frame"><div class="md-table-actions">${renderCopyImageBtn()}${renderExpandBtn('table')}</div><div class="md-table-scroll"><table><thead><tr>${head.map(c => `<th>${c}</th>`).join('')}</tr></thead><tbody>${body.map(r => `<tr>${r.map(c => `<td>${c}</td>`).join('')}</tr>`).join('')}</tbody></table></div></div>`,
    next: i,
  }
}

function fenceLang(line) {
  return String(line || '').trim().slice(3).trim().split(/\s+/, 1)[0] || ''
}

function renderMarkdownBlocks(text) {
  const lines = String(text || '').split(/\r?\n/)
  const blocks = []
  const htmlBuf = []
  let inCode = false
  let code = []
  let codeLang = ''
  let inList = false

  const flushHtml = () => {
    if (htmlBuf.length) {
      blocks.push({ type: 'html', html: htmlBuf.join('') })
      htmlBuf.length = 0
    }
  }
  const pushHtml = (s) => { htmlBuf.push(s) }
  const closeList = () => {
    if (inList) {
      pushHtml('</ul>')
      inList = false
    }
  }
  const emitFence = (body, lang) => {
    if (isMindmapSource(lang, body)) {
      const tree = parseMindmap(body)
      if (tree && String(tree.text || '').trim()) {
        flushHtml()
        blocks.push({ type: 'mindmap', tree })
        return
      }
    }
    pushHtml(renderCodeBlock(body))
  }

  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i]
    if (line.trim().startsWith('```')) {
      if (inCode) {
        emitFence(code.join('\n'), codeLang)
        code = []
        inCode = false
        codeLang = ''
      } else {
        closeList()
        inCode = true
        codeLang = fenceLang(line)
      }
      continue
    }
    if (inCode) {
      code.push(line)
      continue
    }

    const table = renderTable(lines, i)
    if (table) {
      closeList()
      pushHtml(table.html)
      i = table.next - 1
      continue
    }

    if (!line.trim()) {
      closeList()
      continue
    }
    const h = line.match(/^(#{1,4})\s+(.+)$/)
    if (h) {
      closeList()
      const level = h[1].length
      pushHtml(`<h${level}>${inlineMd(h[2])}</h${level}>`)
      continue
    }
    const li = line.match(/^\s*[-*]\s+(.+)$/)
    if (li) {
      if (!inList) {
        pushHtml('<ul>')
        inList = true
      }
      pushHtml(`<li>${inlineMd(li[1])}</li>`)
      continue
    }
    closeList()
    pushHtml(`<p>${inlineMd(line)}</p>`)
  }
  closeList()
  if (inCode) emitFence(code.join('\n'), codeLang)
  flushHtml()
  return blocks
}

const blocks = computed(() => {
  void i18n.locale
  return renderMarkdownBlocks(props.text)
})

const expand = ref(null)
const expandCloseRef = ref(null)
const expandTableRef = ref(null)
const expandMindmapRef = ref(null)
const lightboxCopyLabel = ref('')
let lightboxCopyTimer = null
let lightboxCopyBusy = false

function renderCodeBlock(code) {
  return `<div class="code-wrap"><button class="copy-code" type="button">${escapeHtml(t('common.copy'))}</button><pre><code>${escapeHtml(code)}</code></pre></div>`
}

const rootEl = ref(null)
// path text → { exists: boolean, resolved?: string }
const existsCache = new Map()
let probeQueue = []
let probeTimer = null
let observer = null

function decodeAttr(s) {
  return String(s || '')
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
}

function escapeHtmlAttr(s) {
  return escapeHtml(s)
}

function upgradeCandidate(node, info) {
  if (!node) return
  const path = decodeAttr(node.dataset.path || '')
  node.classList.remove('path-candidate')
  node.classList.add('path-chip')
  node.dataset.state = 'ok'
  node.textContent = pathDisplayName(path) || path
  node.setAttribute('role', 'button')
  node.setAttribute('tabindex', '0')
  if (info && info.resolved) {
    node.dataset.resolved = info.resolved
    node.setAttribute('title', info.resolved)
  } else {
    node.setAttribute('title', path)
  }
}

function downgradeCandidate(node) {
  if (!node) return
  const path = decodeAttr(node.dataset.path || '')
  const textNode = document.createTextNode(path)
  node.replaceWith(textNode)
}

function applyExistsState(path, info) {
  if (!rootEl.value) return
  const sel = `.path-candidate[data-path="${CSS.escape(path)}"], .path-chip[data-path="${CSS.escape(path)}"]`
  const nodes = rootEl.value.querySelectorAll(sel)
  nodes.forEach((node) => {
    if (info && info.exists) {
      upgradeCandidate(node, info)
    } else {
      downgradeCandidate(node)
    }
  })
}

async function probeOne(path) {
  if (!path) return
  if (existsCache.has(path)) {
    applyExistsState(path, existsCache.get(path))
    return
  }
  try {
    const res = await api.pathExists?.(path, props.sessionId || null)
    const info = {
      exists: !!(res && res.exists === true),
      resolved: (res && res.resolved) || null,
    }
    existsCache.set(path, info)
    applyExistsState(path, info)
  } catch {
    const info = { exists: false, resolved: null }
    existsCache.set(path, info)
    applyExistsState(path, info)
  }
}

function flushQueue() {
  probeTimer = null
  const batch = Array.from(new Set(probeQueue))
  probeQueue = []
  batch.forEach((p) => { probeOne(p) })
}

function schedule(path) {
  probeQueue.push(path)
  if (!probeTimer) {
    probeTimer = setTimeout(flushQueue, 60)
  }
}

function scanCandidates() {
  if (!rootEl.value) return
  const nodes = rootEl.value.querySelectorAll('.path-candidate[data-state="probing"]')
  nodes.forEach((node) => {
    const p = decodeAttr(node.dataset.path || '')
    if (p) schedule(p)
  })
  hydratePendingImages()
}

async function hydratePendingImages() {
  if (!rootEl.value) return
  const nodes = rootEl.value.querySelectorAll('img.md-image-pending[data-path]')
  for (const img of nodes) {
    const path = decodeAttr(img.dataset.path || '')
    if (!path || img.dataset.hydrating === '1' || img.getAttribute('src')) continue
    img.dataset.hydrating = '1'
    try {
      const r = await api.readMediaFile(path, props.sessionId || null, 0, 65536)
      if (r?.ok && r.content && (r.kind === 'image' || String(r.content).startsWith('data:image/'))) {
        img.src = r.content
        img.classList.remove('md-image-pending')
        delete img.dataset.hydrating
        continue
      }
    } catch { /* leave the pending frame */ }
    img.classList.add('md-image-missing')
    delete img.dataset.hydrating
  }
}

async function onChipActivate(path) {
  if (!path) return
  try {
    const res = await api.openUserPath(path, props.sessionId || null)
    if (res && res.ok === false) {
      existsCache.set(path, { exists: false, resolved: null })
      applyExistsState(path, { exists: false })
    }
  } catch {
    existsCache.set(path, { exists: false, resolved: null })
    applyExistsState(path, { exists: false })
  }
}

function openTableExpand(html) {
  lightboxCopyLabel.value = ''
  expand.value = { kind: 'table', html }
  nextTick(() => expandCloseRef.value?.focus?.())
}

function flashTableCopyBtn(btn, result) {
  if (!btn) return
  const ok = result === 'copied' || result === 'downloaded'
  const label = result === 'copied'
    ? t('assistant.tableCopiedImage')
    : result === 'downloaded'
      ? t('assistant.tableDownloadedImage')
      : t('assistant.tableCopyImageFail')
  const oldHtml = btn.innerHTML
  const oldTitle = btn.getAttribute('title') || t('assistant.copyTableImage')
  btn.setAttribute('title', label)
  btn.setAttribute('aria-label', label)
  btn.classList.toggle('is-fail', !ok)
  btn.classList.toggle('is-ok', ok)
  if (ok) btn.innerHTML = CHECK_ICON_SVG
  setTimeout(() => {
    btn.innerHTML = oldHtml
    btn.setAttribute('title', oldTitle)
    btn.setAttribute('aria-label', oldTitle)
    btn.classList.remove('is-fail', 'is-ok')
  }, 1400)
}

async function copyTableNode(table, btn) {
  if (!table || btn?.dataset.busy === '1') return
  if (btn) btn.dataset.busy = '1'
  try {
    const result = await copyTableAsImage(table)
    flashTableCopyBtn(btn, result)
    return result
  } catch {
    flashTableCopyBtn(btn, 'fail')
    return 'fail'
  } finally {
    if (btn) delete btn.dataset.busy
  }
}

function applyLightboxCopyResult(result) {
  lightboxCopyLabel.value = result === 'copied'
    ? t('assistant.tableCopiedImage')
    : result === 'downloaded'
      ? t('assistant.tableDownloadedImage')
      : t('assistant.tableCopyImageFail')
  if (lightboxCopyTimer) clearTimeout(lightboxCopyTimer)
  lightboxCopyTimer = setTimeout(() => { lightboxCopyLabel.value = '' }, 1600)
}

async function copyExpandedView() {
  if (lightboxCopyBusy) return
  lightboxCopyBusy = true
  try {
    if (expand.value?.kind === 'table') {
      const table = expandTableRef.value?.querySelector('table')
      if (!table) throw new Error('no table')
      applyLightboxCopyResult(await copyTableAsImage(table))
      return
    }
    if (expand.value?.kind === 'mindmap') {
      applyLightboxCopyResult(await expandMindmapRef.value?.copyAsImage?.() || 'fail')
    }
  } catch {
    applyLightboxCopyResult('fail')
  } finally {
    lightboxCopyBusy = false
  }
}

function openMindmapExpand(tree) {
  expand.value = { kind: 'mindmap', tree }
  nextTick(() => expandCloseRef.value?.focus?.())
}

function closeExpand() {
  expand.value = null
  lightboxCopyLabel.value = ''
  lightboxCopyBusy = false
}

function onExpandKeydown(e) {
  if (e.key === 'Escape' && expand.value) {
    e.preventDefault()
    e.stopPropagation()
    closeExpand()
  }
}

function onRootClick(e) {
  const copyImageBtn = e.target?.closest?.('[data-copy-table-image]')
  if (copyImageBtn) {
    e.preventDefault()
    e.stopPropagation()
    const table = copyImageBtn.closest('.md-table-frame')?.querySelector('table')
    if (table) copyTableNode(table, copyImageBtn)
    return
  }
  const expandBtn = e.target?.closest?.('.md-expand-btn')
  if (expandBtn) {
    e.preventDefault()
    e.stopPropagation()
    const table = expandBtn.closest('.md-table-frame')?.querySelector('table')
    if (table) openTableExpand(table.outerHTML)
    return
  }
  const copyBtn = e.target?.closest?.('.copy-code')
  if (copyBtn) {
    const code = copyBtn.parentElement?.querySelector('code')?.innerText || ''
    navigator.clipboard?.writeText(code)
    const old = copyBtn.textContent
    copyBtn.textContent = t('assistant.copiedShort')
    setTimeout(() => { copyBtn.textContent = old }, 1000)
    return
  }
  const link = e.target?.closest?.('.md-link')
  if (link) {
    e.preventDefault()
    e.stopPropagation()
    const href = decodeAttr(link.dataset.href || link.getAttribute('href') || '')
    if (href) api.openExternalUrl?.(href)
    return
  }
  const img = e.target?.closest?.('.md-image')
  if (img) {
    e.preventDefault()
    e.stopPropagation()
    const src = decodeAttr(img.getAttribute('src') || '')
    const path = decodeAttr(img.dataset.path || '')
    if (src) {
      mediaPreview.openUrl(src, {
        kind: 'image',
        name: decodeAttr(img.getAttribute('alt') || '') || src,
        sessionId: props.sessionId,
      })
    } else if (path) {
      mediaPreview.openFile(path, {
        name: decodeAttr(img.getAttribute('alt') || '') || path,
        sessionId: props.sessionId,
      })
    }
    return
  }
  const chip = e.target?.closest?.('.path-chip')
  if (chip) {
    e.preventDefault()
    e.stopPropagation()
    const p = decodeAttr(chip.dataset.path || '')
    if (isPreviewableMediaPath(p)) {
      mediaPreview.openFile(p, { sessionId: props.sessionId })
    } else {
      onChipActivate(p)
    }
  }
}

function onRootKeydown(e) {
  if (e.key !== 'Enter' && e.key !== ' ') return
  const chip = e.target?.closest?.('.path-chip')
  if (!chip) return
  e.preventDefault()
  const p = decodeAttr(chip.dataset.path || '')
  if (isPreviewableMediaPath(p)) {
    mediaPreview.openFile(p, { sessionId: props.sessionId })
  } else {
    onChipActivate(p)
  }
}

watch(blocks, () => { nextTick(scanCandidates) })

onMounted(() => {
  scanCandidates()
  window.addEventListener('keydown', onExpandKeydown, true)
})

onUnmounted(() => {
  window.removeEventListener('keydown', onExpandKeydown, true)
  if (observer) {
    observer.disconnect()
    observer = null
  }
  if (probeTimer) {
    clearTimeout(probeTimer)
    probeTimer = null
  }
  if (lightboxCopyTimer) {
    clearTimeout(lightboxCopyTimer)
    lightboxCopyTimer = null
  }
})
</script>

<template>
  <div
    ref="rootEl"
    class="markdown-text"
    @click="onRootClick"
    @keydown="onRootKeydown"
  >
    <template v-for="(block, i) in blocks" :key="i">
      <div v-if="block.type === 'html'" class="md-html" v-html="block.html"></div>
      <MindmapView
        v-else-if="block.type === 'mindmap'"
        :tree="block.tree"
        @expand="openMindmapExpand(block.tree)"
      />
    </template>
  </div>
  <Teleport to="body">
    <div
      v-if="expand"
      class="md-lightbox"
      @click.self="closeExpand"
    >
      <section
        class="md-lightbox-shell"
        :class="'kind-' + expand.kind"
        role="dialog"
        aria-modal="true"
        :aria-label="expand.kind === 'mindmap' ? t('assistant.mindmapTitle') : t('assistant.tableTitle')"
      >
        <header class="md-lightbox-head">
          <div>
            <span class="md-lightbox-kicker">{{ expand.kind === 'mindmap' ? t('assistant.mindmapTitle') : t('assistant.tableTitle') }}</span>
            <p v-if="expand.kind === 'mindmap'" class="md-lightbox-hint">{{ t('assistant.mindmapHint') }}</p>
          </div>
          <div class="md-lightbox-actions">
            <button
              v-if="expand.kind === 'table' || expand.kind === 'mindmap'"
              type="button"
              class="md-lightbox-copy"
              :title="t('assistant.copyTableImage')"
              @click="copyExpandedView"
            >{{ lightboxCopyLabel || t('assistant.copyTableImage') }}</button>
            <button
              ref="expandCloseRef"
              type="button"
              class="md-lightbox-close"
              :title="t('assistant.closeExpand')"
              @click="closeExpand"
            >{{ t('assistant.closeExpand') }}</button>
          </div>
        </header>
        <div class="md-lightbox-body">
          <MindmapView
            v-if="expand.kind === 'mindmap'"
            ref="expandMindmapRef"
            variant="overlay"
            :tree="expand.tree"
          />
          <div v-else ref="expandTableRef" class="md-lightbox-table markdown-text" v-html="expand.html"></div>
        </div>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.markdown-text :deep(h1),
.markdown-text :deep(h2),
.markdown-text :deep(h3),
.markdown-text :deep(h4) {
  margin: 8px 0 6px;
  color: var(--fg-strong);
  line-height: 1.35;
}
.markdown-text {
  user-select: text;
  -webkit-user-select: text;
  max-width: 100%;
  min-width: 0;
  overflow-wrap: anywhere;
  word-break: break-word;
}
.markdown-text :deep(*) {
  user-select: text;
  -webkit-user-select: text;
}
.markdown-text :deep(.mm-root),
.markdown-text :deep(.mm-root *) {
  user-select: none;
  -webkit-user-select: none;
}
.markdown-text :deep(h1) { font-size: 17px; }
.markdown-text :deep(h2) { font-size: 15px; }
.markdown-text :deep(h3),
.markdown-text :deep(h4) { font-size: 13px; }
.markdown-text :deep(p) { margin: 6px 0; }
.markdown-text :deep(ul) { margin: 6px 0 6px 18px; padding: 0; }
.markdown-text :deep(li) { margin: 3px 0; }
.markdown-text :deep(code) {
  padding: 1px 5px;
  border-radius: 5px;
  background: var(--bg-sunken);
  border: 1px solid var(--border);
  font-family: "Cascadia Code", Consolas, monospace;
  font-size: 12px;
}
.markdown-text :deep(.code-wrap) {
  position: relative;
}
.markdown-text :deep(.copy-code) {
  position: absolute;
  top: 7px;
  right: 7px;
  padding: 3px 8px;
  border-radius: 7px;
  border: 1px solid var(--border);
  background: var(--panel-2);
  color: var(--muted);
  font-size: 11px;
  cursor: pointer;
  user-select: none;
  -webkit-user-select: none;
}
.markdown-text :deep(.copy-code:hover) {
  color: var(--fg);
  border-color: var(--border-strong);
}
.markdown-text :deep(pre) {
  max-height: 260px;
  max-width: 100%;
  overflow: auto;
  padding: 32px 10px 10px;
  border-radius: 10px;
  background: var(--bg-sunken);
  border: 1px solid var(--border);
  white-space: pre-wrap;
  word-break: break-word;
  overflow-wrap: anywhere;
}
.markdown-text :deep(pre code) {
  padding: 0;
  border: none;
  background: transparent;
}
.markdown-text :deep(.md-html) {
  min-width: 0;
}
.markdown-text :deep(.md-table-frame) {
  position: relative;
  margin: 8px 0;
  border: 1px solid color-mix(in srgb, var(--fg) 18%, var(--border));
  border-radius: 10px;
  background: color-mix(in srgb, var(--fg) 5%, var(--card-bg, var(--panel)));
  overflow: hidden;
}
.markdown-text :deep(.md-table-scroll) {
  max-width: 100%;
  overflow-x: auto;
}
.markdown-text :deep(.md-table-frame table),
.md-lightbox-table :deep(table) {
  display: table;
  width: max-content;
  min-width: 100%;
  margin: 0;
  border-collapse: collapse;
  color: var(--fg);
  font-size: 12px;
}
.markdown-text :deep(th),
.markdown-text :deep(td),
.md-lightbox-table :deep(th),
.md-lightbox-table :deep(td) {
  border: 1px solid color-mix(in srgb, var(--fg) 14%, var(--border));
  padding: 7px 10px;
  color: var(--fg);
  background: transparent;
  text-align: left;
}
.markdown-text :deep(th),
.md-lightbox-table :deep(th) {
  color: var(--fg-strong);
  background: color-mix(in srgb, var(--fg) 9%, var(--panel));
  font-weight: 650;
}
.markdown-text :deep(tbody tr:nth-child(even) td) {
  background: color-mix(in srgb, var(--fg) 4%, transparent);
}
.markdown-text :deep(.md-table-frame thead th:last-child) {
  padding-right: 66px;
}
.markdown-text :deep(.md-table-actions) {
  position: absolute;
  top: 6px;
  right: 6px;
  z-index: 2;
  display: flex;
  gap: 4px;
}
.markdown-text :deep(.md-table-frame .md-table-btn),
.md-lightbox-close,
.md-lightbox-copy {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  padding: 0;
  border-radius: 7px;
  border: 1px solid color-mix(in srgb, var(--fg) 18%, var(--border));
  background: color-mix(in srgb, var(--panel) 86%, transparent);
  color: var(--fg);
  cursor: pointer;
  user-select: none;
  -webkit-user-select: none;
}
.markdown-text :deep(.md-table-frame .md-table-btn:hover),
.md-lightbox-close:hover,
.md-lightbox-copy:hover {
  color: var(--fg-strong);
  border-color: color-mix(in srgb, var(--accent) 45%, var(--border));
  background: var(--panel);
}
.markdown-text :deep(.md-table-frame .md-table-btn.is-ok) {
  color: var(--accent);
  border-color: color-mix(in srgb, var(--accent) 45%, var(--border));
}
.markdown-text :deep(.md-table-frame .md-table-btn.is-fail) {
  color: var(--danger, #e36b6b);
  border-color: color-mix(in srgb, #e36b6b 45%, var(--border));
}
.md-lightbox {
  position: fixed;
  top: var(--titlebar-h, 0);
  right: 0;
  bottom: 0;
  left: 0;
  z-index: 80;
  display: flex;
  align-items: stretch;
  justify-content: stretch;
  padding: 16px;
  background: rgba(8, 10, 14, 0.64);
  backdrop-filter: blur(8px);
}
.md-lightbox-shell {
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  color: var(--fg);
  background: var(--panel);
  border: 1px solid var(--border-strong, var(--border));
  border-radius: 14px;
  box-shadow: var(--shadow-md);
}
.md-lightbox-shell.kind-mindmap {
  flex: 1;
}
.md-lightbox-shell.kind-table {
  width: min(1100px, 100%);
  max-height: 100%;
  margin: auto;
}
.md-lightbox-head {
  flex: 0 0 auto;
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 14px;
  padding: 12px 14px;
  border-bottom: 1px solid var(--border);
}
.md-lightbox-kicker {
  display: block;
  color: var(--fg-strong);
  font-size: 13px;
  font-weight: 650;
}
.md-lightbox-hint {
  margin: 4px 0 0;
  color: var(--muted);
  font-size: 12px;
}
.md-lightbox-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 0 0 auto;
}
.md-lightbox-close,
.md-lightbox-copy {
  width: auto;
  height: 28px;
  padding: 0 10px;
  font-size: 12px;
}
.md-lightbox-body {
  flex: 1;
  min-height: 0;
  overflow: auto;
}
.md-lightbox-shell.kind-mindmap .md-lightbox-body {
  overflow: hidden;
}
.md-lightbox-table {
  padding: 12px 14px 16px;
}
.md-lightbox-table :deep(table) {
  font-size: 13px;
}
.md-lightbox-table :deep(th),
.md-lightbox-table :deep(td) {
  padding: 8px 12px;
}
.md-lightbox-table :deep(tbody tr:nth-child(even) td) {
  background: color-mix(in srgb, var(--fg) 4%, transparent);
}
.markdown-text :deep(.md-image) {
  display: block;
  max-width: 100%;
  max-height: 480px;
  margin: 8px 0;
  border-radius: 10px;
  border: 1px solid var(--border);
  background: var(--bg-sunken);
  cursor: zoom-in;
}
.markdown-text :deep(.md-image:hover) {
  border-color: var(--border-strong, var(--border));
}
.markdown-text :deep(.md-image-pending),
.markdown-text :deep(.md-image-missing) {
  min-width: 72px;
  min-height: 56px;
  background:
    linear-gradient(135deg, color-mix(in srgb, var(--accent) 12%, transparent), transparent 55%),
    var(--bg-sunken);
}
.markdown-text :deep(.md-image-ph) {
  display: inline-block;
  width: 1.7em;
  height: 1.25em;
  margin: 0 3px -2px;
  border-radius: 4px;
  border: 1px solid var(--border);
  background:
    linear-gradient(135deg, color-mix(in srgb, var(--accent) 16%, transparent), transparent 60%),
    var(--bg-sunken);
  vertical-align: baseline;
}
.markdown-text :deep(.md-link) {
  color: var(--md-link-color, var(--accent, #4e8cff));
  text-decoration: none;
  border-bottom: 1px dashed var(
    --md-link-underline,
    color-mix(in srgb, var(--md-link-color, var(--accent, #4e8cff)) 45%, transparent)
  );
  cursor: pointer;
}
.markdown-text :deep(.md-link:hover) {
  border-bottom-style: solid;
}
.markdown-text :deep(.path-candidate) {
  /* Invisible until the backend confirms the path. */
  display: inline;
  color: inherit;
  font: inherit;
  background: none;
  border: 0;
  padding: 0;
  cursor: text;
}
.markdown-text :deep(.path-chip) {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 1px 6px 1px 18px;
  margin: 0 1px;
  border-radius: 5px;
  background: color-mix(in srgb, var(--accent, #4e8cff) 10%, var(--bg-sunken, transparent));
  border: 1px solid color-mix(in srgb, var(--accent, #4e8cff) 28%, var(--border, transparent));
  color: var(--fg-strong, inherit);
  font-family: "Cascadia Code", Consolas, monospace;
  font-size: 12px;
  line-height: 1.5;
  cursor: pointer;
  text-decoration: none;
  position: relative;
  word-break: break-all;
  vertical-align: baseline;
  transition: background 120ms ease, border-color 120ms ease, transform 80ms ease;
}
.markdown-text :deep(.path-chip::before) {
  content: '';
  position: absolute;
  left: 5px;
  top: 50%;
  width: 9px;
  height: 9px;
  margin-top: -5px;
  background: currentColor;
  opacity: 0.55;
  -webkit-mask: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'><path d='M1.5 2.75A.75.75 0 0 1 2.25 2h3.69a.75.75 0 0 1 .53.22l1.06 1.06H13.5A.75.75 0 0 1 14.25 4v8.5a.75.75 0 0 1-.75.75H2.25a.75.75 0 0 1-.75-.75v-9.75z' fill='black'/></svg>") center / contain no-repeat;
          mask: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'><path d='M1.5 2.75A.75.75 0 0 1 2.25 2h3.69a.75.75 0 0 1 .53.22l1.06 1.06H13.5A.75.75 0 0 1 14.25 4v8.5a.75.75 0 0 1-.75.75H2.25a.75.75 0 0 1-.75-.75v-9.75z' fill='black'/></svg>") center / contain no-repeat;
}
.markdown-text :deep(.path-chip:hover) {
  background: color-mix(in srgb, var(--accent, #4e8cff) 20%, var(--bg-sunken, transparent));
  border-color: color-mix(in srgb, var(--accent, #4e8cff) 50%, var(--border, transparent));
}
.markdown-text :deep(.path-chip:active) {
  transform: translateY(0.5px);
}
.markdown-text :deep(.path-chip:focus-visible) {
  outline: 2px solid color-mix(in srgb, var(--accent, #4e8cff) 70%, transparent);
  outline-offset: 1px;
}
.markdown-text :deep(.path-chip.in-code) {
  font-size: 11.5px;
}
</style>
