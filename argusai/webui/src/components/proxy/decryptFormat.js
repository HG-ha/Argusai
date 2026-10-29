// Helpers shared by the inspector's decode pane and the decryption-rule editor.

export const STEP_OPS = ['decode', 'sym', 'rsa', 'decompress', 'slice', 'script']
export const DECODE_CODECS = ['base64', 'base64url', 'hex', 'url']
export const SYM_ALGOS = [
  'aes-cbc', 'aes-ecb', 'aes-ctr', 'aes-gcm',
  'des-cbc', 'des-ecb', '3des-cbc', '3des-ecb',
  'sm4-cbc', 'sm4-ecb', 'rc4', 'xor',
]
export const DECOMPRESS_ALGOS = ['gzip', 'zlib', 'deflate', 'zstd', 'br']
export const KEY_FORMATS = ['utf8', 'hex', 'base64', 'base64url', 'auto']
export const RSA_PADDINGS = ['pkcs1', 'oaep', 'oaep-sha256']
export const IV_FROM_PREFIX = '@prefix'

export const SCRIPT_TEMPLATE = `// input: Buffer (output of the previous step)
// ctx: { part, location, url, method, status, requestHeaders, responseHeaders }
// Available: Buffer, crypto, zlib, TextEncoder, TextDecoder, atob, btoa
function decrypt(input, ctx) {
  const key = Buffer.from('0123456789abcdef', 'utf8')
  const decipher = crypto.createDecipheriv('aes-128-ecb', key, null)
  return Buffer.concat([decipher.update(input), decipher.final()]).toString('utf8')
}
`

export function symNeedsIv(algo) {
  return /-(cbc|ctr|gcm)$/.test(String(algo || ''))
}

export function newStep(op) {
  switch (op) {
    case 'decode': return { op, codec: 'base64' }
    case 'sym': return { op, algo: 'aes-cbc', key: '', key_format: 'utf8', iv: '', iv_format: 'utf8', padding: 'pkcs7' }
    case 'rsa': return { op, private_key: '', padding: 'pkcs1', password: '' }
    case 'decompress': return { op, algo: 'gzip' }
    case 'slice': return { op, start: 0, end: '' }
    case 'script': return { op, code: SCRIPT_TEMPLATE }
    default: return { op }
  }
}

export function newRule() {
  return { id: '', name: '', enabled: true, match: '', apply_to: 'response', field: '', steps: [newStep('decode'), newStep('sym')], note: '' }
}

// Only the fields the backend understands for each op; keeps stale inputs
// (an IV left over from a CBC step switched to ECB) out of saved rules.
export function cleanStep(step) {
  const op = step.op
  const pick = (...keys) => Object.fromEntries(keys.filter((k) => step[k] !== undefined && step[k] !== '').map((k) => [k, step[k]]))
  if (op === 'decode') return { op, ...pick('codec') }
  if (op === 'decompress') return { op, ...pick('algo') }
  if (op === 'rsa') return { op, ...pick('private_key', 'padding', 'password') }
  if (op === 'script') return { op, ...pick('code') }
  if (op === 'slice') {
    const out = { op, start: Number(step.start) || 0 }
    if (step.end !== '' && step.end != null) out.end = Number(step.end)
    return out
  }
  if (op === 'sym') {
    const out = { op, ...pick('algo', 'key', 'key_format', 'padding') }
    if (symNeedsIv(step.algo)) Object.assign(out, pick('iv', 'iv_format'))
    return out
  }
  return { ...step }
}

export function cleanRule(rule) {
  return {
    ...(rule.id ? { id: rule.id } : {}),
    name: String(rule.name || '').trim(),
    enabled: rule.enabled !== false,
    match: String(rule.match || '').trim(),
    apply_to: rule.apply_to || 'response',
    field: String(rule.field || '').trim(),
    steps: (rule.steps || []).map(cleanStep),
    note: String(rule.note || ''),
  }
}

export function stepLabel(step) {
  if (!step) return ''
  if (step.op === 'decode') return step.codec || 'decode'
  if (step.op === 'sym' || step.op === 'decompress') return step.algo || step.op
  if (step.op === 'rsa') return `rsa-${step.padding || 'pkcs1'}`
  if (step.op === 'slice') return `slice[${step.start || 0}:${step.end ?? ''}]`
  return step.op
}

export function stepsSummary(steps) {
  return (steps || []).map(stepLabel).join(' → ')
}

// Auto-decode location ("data.items[3].enc", "form.p", "<body>") → rule field.
// Array indexes widen to [*] so the rule covers every element.
export function fieldFromLocation(location) {
  const loc = String(location || '')
  if (!loc || loc === '<body>') return ''
  return loc.replace(/\[\d+\]/g, '[*]')
}

const LAYER_TO_STEP = {
  base64: { op: 'decode', codec: 'base64' },
  base64url: { op: 'decode', codec: 'base64url' },
  hex: { op: 'decode', codec: 'hex' },
  url: { op: 'decode', codec: 'url' },
  gzip: { op: 'decompress', algo: 'gzip' },
  zlib: { op: 'decompress', algo: 'zlib' },
  zstd: { op: 'decompress', algo: 'zstd' },
}

function symGuess(hint) {
  if (hint === 'block8') return { ...newStep('sym'), algo: 'des-cbc' }
  if (hint === 'stream') return { ...newStep('sym'), algo: 'aes-ctr' }
  return newStep('sym')
}

// Draft rule for a ciphertext the auto-decoder spotted in ``flow``.
export function draftFromDecoded(flow, part, item) {
  let host = ''
  let path = ''
  try {
    const u = new URL(flow.url)
    host = u.hostname
    path = u.pathname
  } catch {}
  const steps = (item?.layers || []).map((l) => LAYER_TO_STEP[l.op]).filter(Boolean).map((s) => ({ ...s }))
  if (item?.cipher_hint === 'openssl-salted') steps.push(newStep('script'))
  else if (item?.cipher_hint) steps.push(symGuess(item.cipher_hint))
  const terms = []
  if (host) terms.push(`host:${host}`)
  if (path && path !== '/') terms.push(`path:"${path.replace(/"/g, '')}"`)
  return {
    ...newRule(),
    name: `${host || 'flow'} ${path || ''}`.trim().slice(0, 80),
    match: terms.join(' '),
    apply_to: part,
    field: fieldFromLocation(item?.location),
    steps: steps.length ? steps : newRule().steps,
  }
}
