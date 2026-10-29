export function emptyCreateDraft() {
  return {
    title: '',
    target_url: '',
    scope: '',
    tagsText: '',
    intent: '',
    // null (not '') so Naive UI NSelect treats "auto" as a real selected option
    target_type: null,
    platform: '',
  }
}

export function emptyCustomHook() {
  return {
    target: '',
    kind: 'function',
    replace_return_src: '',
    replace_get_src: '',
    filter_src: '',
    tag: '',
    note: '',
    capture_stack: true,
  }
}

/** Human-friendly preview for text/json artifacts in the Artifacts tab. */
export function formatArtifactPreview(kind, rawContent) {
  const text = String(rawContent || '')
  const k = String(kind || '')
  if (!text.trim()) return text

  if (k === 'capa_report' || k === 'yara_match') {
    try {
      const data = JSON.parse(text)
      if (k === 'capa_report') {
        const rules = data.rules && typeof data.rules === 'object' ? Object.keys(data.rules) : []
        const meta = data.meta || {}
        const header = [
          `# capa report`,
          meta.format ? `format: ${meta.format}` : '',
          rules.length ? `rules (${rules.length}): ${rules.slice(0, 12).join(', ')}${rules.length > 12 ? '…' : ''}` : '',
        ].filter(Boolean).join('\n')
        return `${header}\n\n${JSON.stringify(data, null, 2)}`
      }
      if (k === 'yara_match') {
        const matches = Array.isArray(data.matches) ? data.matches : []
        const count = data.match_count ?? matches.length
        const header = `# yara matches (${count})\n${matches.slice(0, 20).join('\n')}`
        return `${header}\n\n${JSON.stringify(data, null, 2)}`
      }
    } catch {
      return text
    }
  }

  return text
}

export function staticReportArtifacts(artifacts) {
  const kinds = new Set(['capa_report', 'yara_match'])
  return [...(artifacts || [])]
    .filter((a) => kinds.has(String(a?.kind || '')))
    .sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || '')))
}

export function staticReportLabel(kind) {
  const k = String(kind || '')
  if (k === 'capa_report') return 'capa'
  if (k === 'yara_match') return 'yara'
  return k
}

export function isDecompileIndexArtifact(artifact) {
  const tool = String(artifact?.tool || '')
  const filename = String(artifact?.filename || '')
  const note = String(artifact?.note || '')
  if (filename.startsWith('decompile-')) return true
  if (note.includes('decompile index')) return true
  return tool.includes('jadx') || tool.includes('apktool')
}

export function parseDecompileIndex(rawContent) {
  try {
    const data = JSON.parse(String(rawContent || ''))
    if (!data || typeof data !== 'object') return null
    const outputDir = String(data.output_dir || '').trim()
    if (!outputDir) return null
    return {
      tool: String(data.tool || ''),
      output_dir: outputDir,
      output_dir_rel: String(data.output_dir_rel || ''),
      file_count: Number(data.tree?.file_count || 0),
      partial: !!data.partial,
      ok: data.ok !== false,
    }
  } catch {
    return null
  }
}

export function formatDecompileIndexPreview(rawContent) {
  const info = parseDecompileIndex(rawContent)
  if (!info) return String(rawContent || '')
  const label = info.tool.includes('jadx') ? 'JADX' : info.tool.includes('apktool') ? 'APKTool' : info.tool || 'decompile'
  const lines = [
    `# ${label} decompile index`,
    `output_dir: ${info.output_dir}`,
    info.output_dir_rel ? `folder: decompiled/${info.output_dir_rel}` : '',
    info.file_count ? `files: ${info.file_count}` : '',
    info.partial ? 'status: partial (usable)' : '',
  ].filter(Boolean)
  return `${lines.join('\n')}\n\n${String(rawContent || '')}`
}
