// AI UI Protocol v2 — frontend normalization layer.
//
// Mirror of `argusai/core/assistant/ui_protocol.py`. Schemas live in
// `docs/AI-UI-PROTOCOL.md`.
//
// All functions are pure and idempotent: they return a *new* object with
// `id` / `version` / `source` / typed `payload` filled in, never mutating
// the input. Legacy flat blocks/actions are preserved alongside the new
// fields so existing Vue cards keep working until they're migrated.

import { t } from './i18n.js'

export const PROTOCOL_VERSION = 2
export const BLOCK_VERSION_DEFAULT = 1

export const BLOCK_SOURCES = Object.freeze(['tool', 'model', 'runtime', 'system'])

export const ACTION_TYPES = Object.freeze([
  'send_text',
  'open_route',
  'open_external',
  'open_attachment',
  'open_media',
  'tool_decision',
  'user_decision',
  'custom',
])

export const STREAM_EVENT_KINDS = Object.freeze([
  'text_chunk',
  'block_upsert',
  'tool_activity',
  'tool_complete',
  'run_done',
  'run_error',
  'stage',
  'ping',
])

export const ATTACHMENT_KINDS = Object.freeze(['image', 'video', 'audio', 'file'])

export const SELECTION_REFERENCE_KINDS = Object.freeze([
  'image_attachment',
  'message',
  'file_selection',
])

const SELECTION_REFERENCE_KINDS_SET = new Set(SELECTION_REFERENCE_KINDS)

// Headers that legacy v1 user messages used to embed in `content`. Kept in
// sync with `_REFERENCE_HEADERS` in `ui_protocol.py`.
const REFERENCE_CONTEXT_HEADERS = Object.freeze([
  '选区上下文',
  '图片引用上下文',
  '对话引用上下文',
])
const REFERENCE_CONTEXT_TAIL_RE = new RegExp(
  `\\n{0,2}\\[(?:${REFERENCE_CONTEXT_HEADERS.join('|')})\\][\\s\\S]*$`,
  'u',
)

const BLOCK_SOURCE_MAP = Object.freeze({
  task_progress: 'runtime',
  findings_summary: 'runtime',
  confirm: 'runtime',
  tool_confirm: 'runtime',
  user_decision: 'runtime',
  user_decision_batch: 'runtime',
  todo_list: 'runtime',
  kb_updated: 'runtime',
  tool_activity: 'runtime',
  tool_trace: 'runtime',
  scan_plan: 'model',
  command_plan: 'model',
  data_table: 'model',
  structured_card: 'model',
  file_resource_view: 'model',
  probe_result: 'tool',
  command_result: 'tool',
  channel_delivery: 'tool',
  reverse_result: 'tool',
  runtime_trace: 'tool',
  sandbox_file: 'tool',
  sandbox_run: 'tool',
  sandbox_env: 'tool',
  vulnerability_evidence: 'tool',
  attack_chain: 'tool',
  request_replay: 'tool',
  oob_event: 'tool',
  media_preview: 'tool',
  attachment_preview: 'tool',
})

const BLOCK_SOURCES_SET = new Set(BLOCK_SOURCES)
const ACTION_TYPES_SET = new Set(ACTION_TYPES)
const ATTACHMENT_KINDS_SET = new Set(ATTACHMENT_KINDS)

let idCounter = 0
function generateId(prefix = 'blk') {
  idCounter = (idCounter + 1) | 0
  const rand = Math.floor(Math.random() * 0xffffffff).toString(16).padStart(8, '0')
  return `${prefix}_${rand}${idCounter.toString(16).padStart(2, '0')}`
}

function isPlainObject(v) {
  return v !== null && typeof v === 'object' && !Array.isArray(v)
}

// JSON-style canonical serialization with sorted object keys, used as
// input to `stableBlockHash`. Drops keys that are either auto-injected
// by `normalizeBlock` (id, version, source) or known to wobble between
// equivalent emissions (ts, timestamp, request_id) so two semantically
// identical blocks serialize to the same string.
const AUTO_FIELDS_FOR_HASH = new Set([
  'id', 'version', 'source',
  'ts', 'timestamp', 'created_at', 'request_id',
])
function stableSerialize(value) {
  if (value === null || value === undefined) return 'null'
  const t = typeof value
  if (t === 'number' || t === 'boolean') return JSON.stringify(value)
  if (t === 'string') return JSON.stringify(value)
  if (Array.isArray(value)) {
    return '[' + value.map(stableSerialize).join(',') + ']'
  }
  if (t === 'object') {
    const keys = Object.keys(value).filter(k => !AUTO_FIELDS_FOR_HASH.has(k)).sort()
    return '{' + keys.map(k => JSON.stringify(k) + ':' + stableSerialize(value[k])).join(',') + '}'
  }
  return JSON.stringify(String(value))
}

// 64-bit FNV-style content hash, returned as a base36 string. Used to
// derive deterministic `block.id` values when the backend did not
// supply one — so the *same* block emitted twice (e.g. streamed via
// `block_upsert` AND committed inside `ChatResponse.blocks`) collapses
// onto a single id and dedup-by-id in `mergeBlocks` actually works.
//
// Collision probability is negligible at our scale (~1e-12 per round);
// even a false positive only means we'd merge two unrelated blocks,
// which is strictly safer than the current "duplicate the card" bug.
function stableBlockHash(block) {
  try {
    const json = stableSerialize(block)
    let h1 = 0x811c9dc5
    let h2 = 0x9e3779b9
    for (let i = 0; i < json.length; i += 1) {
      const c = json.charCodeAt(i)
      h1 = Math.imul(h1 ^ c, 16777619) | 0
      h2 = Math.imul(h2 ^ c, 2246822519) | 0
    }
    const left = (h1 >>> 0).toString(36).padStart(7, '0')
    const right = (h2 >>> 0).toString(36).padStart(7, '0')
    return left + right
  } catch (e) {
    return ''
  }
}

export function inferBlockSource(blockType) {
  if (!blockType || typeof blockType !== 'string') return 'tool'
  return BLOCK_SOURCE_MAP[blockType] || 'tool'
}

export function normalizeBlock(block) {
  if (!isPlainObject(block)) return block
  const out = { ...block }
  const blockType = out.type || ''
  if (!out.id || typeof out.id !== 'string') {
    // Content-derived id. Two normalize() calls on the same backend
    // block (e.g. one via streaming block_upsert, one via the final
    // ChatResponse.blocks) MUST land on the same id, otherwise the
    // id-based dedup in `mergeBlocks` is silently defeated and the
    // user sees the card twice. Falling back to a random id only when
    // hashing fails preserves the original behavior for pathological
    // inputs (cyclic objects, etc).
    const hash = stableBlockHash(out)
    out.id = hash ? `blk_${hash}` : generateId('blk')
  }
  if (typeof out.version !== 'number' || !Number.isFinite(out.version)) {
    out.version = BLOCK_VERSION_DEFAULT
  }
  if (!out.source || !BLOCK_SOURCES_SET.has(out.source)) {
    out.source = inferBlockSource(blockType)
  }
  if (Array.isArray(out.actions)) {
    out.actions = out.actions.map(a => normalizeAction(a))
  }
  if (isPlainObject(out.payload)) {
    const payload = { ...out.payload }
    if (Array.isArray(payload.actions)) {
      payload.actions = payload.actions.map(a => normalizeAction(a))
    }
    out.payload = payload
  } else if (blockType === 'confirm') {
    // Mirror of `_derive_confirm_payload` in `ui_protocol.py`: give v2-aware
    // readers a typed `payload.actions` for legacy confirm blocks without
    // touching the top-level fields that older cards still read.
    out.payload = deriveConfirmPayload(out)
  }
  return out
}

function deriveConfirmPayload(block) {
  const payload = {
    title: block.title,
    description: block.body || block.description,
    tool_name: block.name,
    confirm_id: block.confirm_id,
    permission_key: block.permission_key,
    risk: block.risk,
    arguments: block.arguments,
  }
  const actions = []
  if (typeof block.confirm_value === 'string' && block.confirm_value) {
    actions.push({
      id: generateId('act'),
      type: 'tool_decision',
      label: t('subAgentGroup.allowOnce'),
      variant: 'primary',
      payload: {
        decision: 'allow_once',
        value: block.confirm_value,
        confirm_id: block.confirm_id,
      },
    })
  }
  for (const extra of (Array.isArray(block.actions) ? block.actions : [])) {
    if (!isPlainObject(extra)) continue
    actions.push(normalizeAction(extra))
  }
  if (typeof block.cancel_value === 'string' && block.cancel_value) {
    actions.push({
      id: generateId('act'),
      type: 'tool_decision',
      label: t('assistant.deny'),
      variant: 'danger',
      payload: {
        decision: 'deny',
        value: block.cancel_value,
        confirm_id: block.confirm_id,
      },
    })
  }
  payload.actions = actions
  const clean = {}
  for (const k of Object.keys(payload)) {
    if (payload[k] !== undefined && payload[k] !== null) clean[k] = payload[k]
  }
  return clean
}

export function normalizeBlocks(blocks) {
  if (!Array.isArray(blocks)) return []
  return blocks.map(b => normalizeBlock(b))
}

function coerceActionType(raw) {
  const existing = raw.type
  const payload = raw.payload
  if (typeof existing === 'string' && ACTION_TYPES_SET.has(existing)) {
    return {
      type: existing,
      payload: isPlainObject(payload) ? { ...payload } : {},
    }
  }
  const confirm = raw.confirm_value
  if (typeof confirm === 'string' && confirm) {
    const p = { decision: confirm }
    if (raw.permission_key) p.permission_key = raw.permission_key
    if (raw.confirm_id) p.confirm_id = raw.confirm_id
    return { type: 'tool_decision', payload: p }
  }
  const cancel = raw.cancel_value
  if (typeof cancel === 'string' && cancel) {
    const p = { decision: cancel }
    if (raw.confirm_id) p.confirm_id = raw.confirm_id
    return { type: 'tool_decision', payload: p }
  }
  const href = raw.href
  if (typeof href === 'string' && href) {
    if (href.startsWith('#/') || href.startsWith('/')) {
      return { type: 'open_route', payload: { href } }
    }
    return { type: 'open_external', payload: { href } }
  }
  const value = raw.value
  if (typeof value === 'string' && value) {
    return { type: 'send_text', payload: { value } }
  }
  return { type: 'custom', payload: isPlainObject(payload) ? { ...payload } : {} }
}

export function normalizeAction(action) {
  if (!isPlainObject(action)) return action
  const out = { ...action }
  const { type, payload } = coerceActionType(out)
  if (!out.id || typeof out.id !== 'string') {
    out.id = generateId('act')
  }
  out.type = type
  if (Object.keys(payload).length > 0 || !isPlainObject(out.payload)) {
    out.payload = payload
  }
  return out
}

function inferAttachmentKind(att) {
  const rawKind = att.kind
  if (typeof rawKind === 'string' && ATTACHMENT_KINDS_SET.has(rawKind)) return rawKind
  const mime = att.mime || att.content_type || ''
  if (typeof mime === 'string') {
    if (mime.startsWith('image/')) return 'image'
    if (mime.startsWith('video/')) return 'video'
    if (mime.startsWith('audio/')) return 'audio'
  }
  const name = att.name || ''
  if (typeof name === 'string') {
    const lower = name.toLowerCase()
    if (/\.(png|jpe?g|gif|webp|bmp|svg)$/.test(lower)) return 'image'
    if (/\.(mp4|mov|webm|mkv)$/.test(lower)) return 'video'
    if (/\.(mp3|wav|ogg|flac)$/.test(lower)) return 'audio'
  }
  return 'file'
}

export function normalizeAttachment(att) {
  if (!isPlainObject(att)) return att
  const out = { ...att }
  if (!out.id || typeof out.id !== 'string') {
    out.id = generateId('att')
  }
  if (!out.kind || !ATTACHMENT_KINDS_SET.has(out.kind)) {
    out.kind = inferAttachmentKind(out)
  }
  if (!out.url) {
    for (const fallback of ['media_url', 'path', 'uri']) {
      const v = out[fallback]
      if (typeof v === 'string' && v) {
        out.url = v
        break
      }
    }
  }
  if (!out.data_url) {
    for (const fallback of ['dataUrl', 'base64', 'data']) {
      const v = out[fallback]
      if (typeof v === 'string' && v.startsWith('data:')) {
        out.data_url = v
        break
      }
    }
  }
  if (!out.source) out.source = 'user'
  return out
}

export function normalizeVoice(voice) {
  if (!isPlainObject(voice)) return null
  const data = voice.data_base64 || voice.data || ''
  const sandboxRel = String(voice.sandbox_rel || voice.sandbox_path || '').trim()
  if ((!data || typeof data !== 'string') && !sandboxRel) return null
  const out = { ...voice }
  if (!out.id || typeof out.id !== 'string') {
    out.id = generateId('voice')
  }
  if (!out.type) out.type = 'audio'
  if (!out.mime) out.mime = 'audio/webm'
  if (data && !out.data_base64) out.data_base64 = data
  if (sandboxRel && !out.sandbox_rel) {
    const normalized = sandboxRel.replace(/\\/g, '/').replace(/^\/+/, '')
    const marker = '/_voice/'
    const pos = normalized.lastIndexOf(marker)
    out.sandbox_rel = pos >= 0 ? normalized.slice(pos + 1) : `_voice/${normalized.split('/').pop()}`
  }
  return out
}

export function normalizeSelectionReference(ref) {
  if (!isPlainObject(ref)) return null
  const out = { ...ref }
  let kind = out.kind
  if (typeof kind !== 'string' || !SELECTION_REFERENCE_KINDS_SET.has(kind)) {
    if (out.attachment_name || out.attachment_id) kind = 'image_attachment'
    else if (Number.isInteger(out.message_index) || out.role === 'user' || out.role === 'assistant') kind = 'message'
    else if (out.file_path || out.file_name || out.start_line) kind = 'file_selection'
    else return null
    out.kind = kind
  }
  return out
}

export function deriveSelectionReferenceFromContent(content) {
  if (typeof content !== 'string') return null
  if (content.includes('[图片引用上下文]')) {
    const ref = { kind: 'image_attachment' }
    const source = content.match(/^来源:\s*(.+)$/mu)
    if (source) {
      const s = source[1].trim()
      if (s.startsWith('AI')) ref.role = 'assistant'
      else if (s.startsWith('用户')) ref.role = 'user'
      const ordinal = s.match(/#(\d+)$/)
      if (ordinal) ref.message_index = Math.max(0, parseInt(ordinal[1], 10) - 1)
    }
    const name = content.match(/^图片:\s*(.+)$/mu)
    if (name) ref.attachment_name = name[1].trim()
    const mime = content.match(/^类型:\s*(.+)$/mu)
    if (mime) ref.attachment_mime = mime[1].trim()
    return ref
  }
  if (content.includes('[对话引用上下文]')) {
    const ref = { kind: 'message' }
    const role = content.match(/^来源:\s*(.+)$/mu)
    if (role) {
      const r = role[1].trim()
      ref.role = r === 'assistant' ? 'assistant' : 'user'
    }
    const body = content.match(/```text\n([\s\S]*?)\n```/u)
    if (body) ref.text = body[1]
    return ref
  }
  if (content.includes('[选区上下文]')) {
    const ref = { kind: 'file_selection' }
    const file = content.match(/^文件:\s*(.+)$/mu)
    if (file) ref.file_path = file[1].trim()
    const range = content.match(/^范围:\s*L(\d+):C(\d+) - L(\d+):C(\d+)$/mu)
    if (range) {
      ref.start_line = parseInt(range[1], 10)
      ref.start_column = parseInt(range[2], 10)
      ref.end_line = parseInt(range[3], 10)
      ref.end_column = parseInt(range[4], 10)
    }
    const body = content.match(/```text\n([\s\S]*?)\n```/u)
    if (body) ref.text = body[1]
    return ref
  }
  return null
}

export function stripSelectionReferenceFromContent(content) {
  if (typeof content !== 'string') return content
  return content.replace(REFERENCE_CONTEXT_TAIL_RE, '').replace(/\s+$/u, '')
}

// Compact human label for the chip rendered in the input area / above message.
// Mirrors the UI's `selectionReferenceFromText` heuristics but works on the
// structured form directly. Returns '' if no usable label can be produced.
export function selectionReferenceLabel(ref) {
  if (!isPlainObject(ref)) return ''
  if (ref.kind === 'image_attachment') {
    const name = ref.attachment_name || ''
    return name ? t('assistant.imageReferenceNamed', { name }) : t('assistant.imageReference')
  }
  if (ref.kind === 'message') {
    if (ref.role === 'assistant') return t('assistant.assistantReply')
    if (ref.role === 'user') return t('assistant.userMessage')
    return t('assistant.conversationReference')
  }
  if (ref.kind === 'file_selection') {
    const file = ref.file_name || ref.file_path || ''
    let range = ''
    if (ref.start_line) {
      range = `L${ref.start_line}:C${ref.start_column} - L${ref.end_line}:C${ref.end_column}`
    }
    return [file, range].filter(Boolean).join(' · ')
  }
  return ''
}

export function normalizeMessage(msg) {
  if (!isPlainObject(msg)) return msg
  const out = { ...msg }
  if (Array.isArray(out.blocks)) out.blocks = normalizeBlocks(out.blocks)
  if (Array.isArray(out.attachments)) {
    out.attachments = out.attachments.map(a => normalizeAttachment(a))
  }
  if (out.voice) {
    out.voice = normalizeVoice(out.voice)
  }
  if (out.selection_reference) {
    out.selection_reference = normalizeSelectionReference(out.selection_reference)
  } else if (out.role === 'user' && typeof out.content === 'string') {
    const derived = deriveSelectionReferenceFromContent(out.content)
    if (derived) {
      out.selection_reference = derived
      out.content = stripSelectionReferenceFromContent(out.content)
    }
  }
  if (!out._protocol_version) out._protocol_version = PROTOCOL_VERSION
  return out
}

export function normalizeStreamEvent(event) {
  if (!isPlainObject(event)) return event
  const out = { ...event }
  if (!out.kind || typeof out.kind !== 'string') out.kind = 'text_chunk'
  if (typeof out.ts !== 'number') out.ts = Date.now() / 1000
  if (out.kind === 'block_upsert' && isPlainObject(out.data) && isPlainObject(out.data.block)) {
    out.data = { ...out.data, block: normalizeBlock(out.data.block) }
  }
  return out
}

// Return a flat view that merges payload into top-level keys so callers can
// uniformly read `view.title`/`view.rows` regardless of legacy/v2 shape.
export function blockPayloadView(block) {
  if (!isPlainObject(block)) return {}
  if (isPlainObject(block.payload)) {
    const { payload, ...rest } = block
    return { ...rest, ...payload }
  }
  return { ...block }
}
