// Helpers shared by the rewrite-rule editor and list card. Mirrors the
// backend proxy_rewrite action set.

export const APPLY_TO = ['request', 'response', 'both']
export const REQUEST_OPS = ['set_header', 'remove_header', 'set_query', 'remove_query', 'set_method', 'replace_body', 'short_circuit']
export const RESPONSE_OPS = ['set_header', 'remove_header', 'set_status', 'replace_body']

// Which ops are legal for a given apply_to (an action must be valid for every part).
export function opsFor(applyTo) {
  if (applyTo === 'request') return REQUEST_OPS
  if (applyTo === 'response') return RESPONSE_OPS
  return REQUEST_OPS.filter((op) => RESPONSE_OPS.includes(op))  // both
}

export function newAction(op) {
  switch (op) {
    case 'set_header': return { op, name: '', value: '' }
    case 'remove_header': return { op, name: '' }
    case 'set_query': return { op, name: '', value: '' }
    case 'remove_query': return { op, name: '' }
    case 'set_method': return { op, method: 'GET' }
    case 'set_status': return { op, status: 200 }
    case 'replace_body': return { op, find: '', replace: '', regex: false, count: 0 }
    case 'short_circuit': return { op, status: 200, headers: {}, body: '' }
    default: return { op }
  }
}

export function newRule() {
  return {
    id: '', name: '', enabled: true, match: '', apply_to: 'request',
    actions: [newAction('set_header')], note: '',
  }
}

export function cleanAction(action) {
  const op = action.op
  const num = (v) => { const n = Number(v); return Number.isFinite(n) ? n : 0 }
  if (op === 'set_header') return { op, name: String(action.name || ''), value: String(action.value || '') }
  if (op === 'remove_header') return { op, name: String(action.name || '') }
  if (op === 'set_query') return { op, name: String(action.name || ''), value: String(action.value || '') }
  if (op === 'remove_query') return { op, name: String(action.name || '') }
  if (op === 'set_method') return { op, method: String(action.method || 'GET') }
  if (op === 'set_status') return { op, status: num(action.status) }
  if (op === 'replace_body') {
    const out = { op, find: String(action.find || ''), replace: String(action.replace || '') }
    if (action.regex) out.regex = true
    if (num(action.count) > 0) out.count = num(action.count)
    return out
  }
  if (op === 'short_circuit') {
    const headers = action.headers && typeof action.headers === 'object' ? action.headers : {}
    return { op, status: num(action.status) || 200, headers, body: String(action.body || '') }
  }
  return { ...action }
}

export function cleanRule(rule) {
  return {
    ...(rule.id ? { id: rule.id } : {}),
    name: String(rule.name || '').trim(),
    enabled: rule.enabled !== false,
    match: String(rule.match || '').trim(),
    apply_to: APPLY_TO.includes(rule.apply_to) ? rule.apply_to : 'request',
    actions: (rule.actions || []).map(cleanAction),
    note: String(rule.note || ''),
  }
}

export function actionLabel(action) {
  if (!action) return ''
  const op = action.op
  if (op === 'set_header') return `set ${action.name || 'header'}`
  if (op === 'remove_header') return `rm ${action.name || 'header'}`
  if (op === 'set_query') return `set ?${action.name || ''}`
  if (op === 'remove_query') return `rm ?${action.name || ''}`
  if (op === 'set_method') return `method ${action.method || ''}`
  if (op === 'set_status') return `status ${action.status ?? ''}`
  if (op === 'replace_body') return action.regex ? 're/body/' : 'body'
  if (op === 'short_circuit') return `block ${action.status ?? 200}`
  return op
}

export function actionsSummary(actions) {
  return (actions || []).map(actionLabel).join(' · ')
}

// Draft a request-side rule scoped to one flow's host + path.
export function draftFromFlow(flow) {
  let host = ''
  let path = ''
  try {
    const u = new URL(flow.url)
    host = u.hostname
    path = u.pathname
  } catch {}
  const terms = []
  if (host) terms.push(`host:${host}`)
  if (path && path !== '/') terms.push(`path:"${path.replace(/"/g, '')}"`)
  return {
    ...newRule(),
    name: `${host || 'flow'} ${path || ''}`.trim().slice(0, 80),
    match: terms.join(' '),
    apply_to: 'request',
  }
}
