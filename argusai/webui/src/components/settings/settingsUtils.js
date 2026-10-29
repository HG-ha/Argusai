export function formatBytes(n) {
  const v = Number(n) || 0
  if (v < 1024) return `${v} B`
  if (v < 1024 * 1024) return `${(v / 1024).toFixed(1)} KiB`
  if (v < 1024 * 1024 * 1024) return `${(v / (1024 * 1024)).toFixed(1)} MiB`
  return `${(v / (1024 * 1024 * 1024)).toFixed(1)} GiB`
}

export function pathSep(p) {
  // Heuristic: if the string contains a backslash treat it as Windows-style.
  return String(p || '').includes('\\') ? '\\' : '/'
}

export function joinPath(root, sub) {
  const r = String(root || '').replace(/[\\/]+$/, '')
  if (!r) return ''
  return r + pathSep(r) + String(sub || '')
}
