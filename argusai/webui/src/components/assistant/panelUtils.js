export const RENDER_WINDOW_SIZE = 60
export const RENDER_WINDOW_TRIGGER = 80
export const RENDER_WINDOW_STEP = 60

export const ATTACHMENT_MAX_COUNT = 8
export const ATTACHMENT_MAX_BYTES = 8 * 1024 * 1024
export const ATTACHMENT_ACCEPT_MIMES = ['image/png', 'image/jpeg', 'image/webp', 'image/gif']
export const ATTACHMENT_AUDIO_ACCEPT_MIMES = [
  'audio/wav',
  'audio/x-wav',
  'audio/mpeg',
  'audio/mp3',
  'audio/ogg',
  'audio/webm',
  'audio/mp4',
  'audio/m4a',
  'audio/x-m4a',
]
export const ATTACHMENT_VIDEO_MAX_BYTES = 32 * 1024 * 1024
export const ATTACHMENT_FILE_MAX_BYTES = 25 * 1024 * 1024
export const ATTACHMENT_VIDEO_ACCEPT_MIMES = [
  'video/mp4',
  'video/webm',
  'video/quicktime',
  'video/x-matroska',
  'video/x-msvideo',
  'video/x-m4v',
]
export const ATTACHMENT_TEXT_MAX_BYTES = 4 * 1024 * 1024
export const ATTACHMENT_TEXT_MIME_PREFIXES = ['text/', 'application/json', 'application/xml', 'application/yaml', 'application/x-yaml', 'application/javascript', 'application/x-javascript', 'application/x-shellscript', 'application/x-python', 'application/sql']
export const ATTACHMENT_TEXT_EXTENSIONS = new Set([
  'md', 'markdown', 'mdx', 'rst', 'txt', 'log', 'csv', 'tsv',
  'json', 'jsonl', 'ndjson', 'yaml', 'yml', 'toml', 'ini', 'cfg', 'env',
  'xml', 'html', 'htm', 'svg',
  'js', 'mjs', 'cjs', 'jsx', 'ts', 'tsx',
  'py', 'pyi', 'pyw',
  'sh', 'bash', 'zsh', 'fish', 'ps1', 'bat', 'cmd',
  'go', 'rs', 'java', 'kt', 'kts', 'rb', 'php', 'pl', 'cs', 'swift',
  'c', 'cc', 'cpp', 'cxx', 'h', 'hpp',
  'sql', 'graphql', 'gql',
  'css', 'scss', 'sass', 'less',
  'vue', 'svelte',
  'dockerfile', 'makefile', 'cmake', 'gradle',
  'patch', 'diff',
  'lock',
  'har', 'pcap',
])

export function fileNameExt(name) {
  if (!name) return ''
  const m = String(name).toLowerCase().match(/\.([a-z0-9]+)$/)
  return m ? m[1] : ''
}

export function isTextLikeFile(file) {
  const mime = (file.type || '').toLowerCase()
  if (mime.startsWith('image/')) return false
  if (mime) {
    for (const pref of ATTACHMENT_TEXT_MIME_PREFIXES) {
      if (mime === pref || mime.startsWith(pref)) return true
    }
  }
  return ATTACHMENT_TEXT_EXTENSIONS.has(fileNameExt(file.name || ''))
}

export function isImageFile(file) {
  let mime = (file.type || '').toLowerCase()
  if (mime === 'image/jpg') mime = 'image/jpeg'
  if (ATTACHMENT_ACCEPT_MIMES.includes(mime)) return true
  const ext = fileNameExt(file.name || '')
  return ['png', 'jpg', 'jpeg', 'webp', 'gif'].includes(ext)
}

export function normalizeAudioMime(raw) {
  let mime = String(raw || '').toLowerCase().split(';')[0].trim()
  if (mime === 'audio/x-wav') mime = 'audio/wav'
  if (mime === 'audio/mp3') mime = 'audio/mpeg'
  return mime
}

export function isAudioFile(file) {
  const mime = normalizeAudioMime(file?.type || '')
  if (mime && (ATTACHMENT_AUDIO_ACCEPT_MIMES.includes(mime) || mime.startsWith('audio/'))) return true
  const ext = fileNameExt(file?.name || '')
  return ['wav', 'mp3', 'mpeg', 'ogg', 'webm', 'm4a', 'aac', 'flac'].includes(ext)
}

const VIDEO_EXTENSION_MIMES = {
  mp4: 'video/mp4',
  webm: 'video/webm',
  mov: 'video/quicktime',
  mkv: 'video/x-matroska',
  avi: 'video/x-msvideo',
  m4v: 'video/x-m4v',
}
const VIDEO_MIME_ALIASES = {
  'video/mov': 'video/quicktime',
  'video/x-quicktime': 'video/quicktime',
  'video/mkv': 'video/x-matroska',
  'video/matroska': 'video/x-matroska',
  'video/avi': 'video/x-msvideo',
  'video/msvideo': 'video/x-msvideo',
  'video/m4v': 'video/x-m4v',
}
// Drag/drop and clipboard files often arrive with an empty or generic type in
// WebView builds, so the extension is the only signal left in those cases.
const VIDEO_UNKNOWN_MIMES = new Set(['', 'application/octet-stream', 'binary/octet-stream'])

export function normalizeVideoMime(raw, filename = '') {
  let mime = String(raw || '').toLowerCase().split(';')[0].trim()
  if (VIDEO_MIME_ALIASES[mime]) mime = VIDEO_MIME_ALIASES[mime]
  if (ATTACHMENT_VIDEO_ACCEPT_MIMES.includes(mime)) return mime
  if (VIDEO_UNKNOWN_MIMES.has(mime) || mime.startsWith('video/')) {
    const fromExt = VIDEO_EXTENSION_MIMES[fileNameExt(filename)]
    if (fromExt) return fromExt
  }
  return mime
}

export function isVideoFile(file) {
  const raw = String(file?.type || '').toLowerCase().split(';')[0].trim()
  if (!VIDEO_UNKNOWN_MIMES.has(raw) && !raw.startsWith('video/') && !VIDEO_MIME_ALIASES[raw]) return false
  const mime = normalizeVideoMime(raw, file?.name || '')
  return ATTACHMENT_VIDEO_ACCEPT_MIMES.includes(mime) || mime.startsWith('video/')
}

export function bytesToBase64(bytes) {
  let bin = ''
  for (let i = 0; i < bytes.length; i += 1) bin += String.fromCharCode(bytes[i])
  return btoa(bin)
}

export function formatVoiceMs(ms) {
  const sec = Math.max(0, Math.floor((Number(ms) || 0) / 1000))
  const mm = Math.floor(sec / 60)
  const ss = sec % 60
  return `${mm}:${String(ss).padStart(2, '0')}`
}

/** WeChat-style short label, e.g. 3" */
export function formatVoiceSecondsLabel(ms) {
  const sec = Math.max(1, Math.ceil((Number(ms) || 0) / 1000))
  return `${sec}"`
}

/** Bubble width grows slightly with duration (px). */
export function voiceBubbleWidth(ms) {
  const sec = Math.max(1, Math.ceil((Number(ms) || 0) / 1000))
  const w = Math.min(220, Math.max(76, 52 + sec * 14))
  return `${w}px`
}

export function humanSize(n) {
  if (n == null) return ''
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

export const INPUT_PATH_TOKEN_RE = /(?:[A-Za-z]:[\\/][^\s`<>"']+|\/[^\s`<>"']+\/[^\s`<>"']+|(?:[A-Za-z0-9_.\-\u4e00-\u9fff]+[\\/]){1,}[A-Za-z0-9_.\-\u4e00-\u9fff]+|[A-Za-z0-9_.\-\u4e00-\u9fff]+\.[A-Za-z0-9]{1,8})/g

export function trimPathToken(raw) {
  return String(raw || '').replace(/[,;:.!?)\]}>'"`]+$/, '')
}

export function pathDisplayName(raw) {
  const s = trimPathToken(raw).trim().replace(/[\\/]+$/, '')
  if (!s) return ''
  const parts = s.split(/[\\/]/).filter(Boolean)
  return parts[parts.length - 1] || s
}

export function looksLikeInputPath(raw) {
  const s = trimPathToken(raw).trim()
  if (s.length < 3 || /^https?:\/\//i.test(s) || /^[a-z][a-z0-9+.-]*:\/\//i.test(s)) return false
  if (/^[A-Za-z]:[\\/]/.test(s)) return true
  if (s.startsWith('/') && /\/[^\s/]+/.test(s.slice(1))) return true
  if (/[\\/]/.test(s)) return true
  return /\.[A-Za-z0-9]{1,8}$/.test(s)
}
