// VSCode-flavoured file-type chips for the sandbox browser.
//
// We deliberately don't pull in an icon font: every chip is a 1-3 character
// label painted on a tinted background, which scales cleanly with the rest of
// the theme tokens and works offline (the app runs in pywebview with no CDN
// access).
//
// File-type chips stay data-only. Badge copy goes through i18n so EN/ZH
// switch with the rest of the UI.

import { t } from './i18n.js'

// Curated mapping from extension → { label, hue }.
// `hue` is an HSL hue value used by the chip background; we keep saturation /
// lightness fixed so the palette is visually consistent.
const EXT_PRESETS = Object.freeze({
  // languages / scripts
  py: { label: 'PY', hue: 207 },
  pyc: { label: 'PYC', hue: 207 },
  ipynb: { label: 'NB', hue: 30 },
  js: { label: 'JS', hue: 50 },
  mjs: { label: 'MJS', hue: 50 },
  ts: { label: 'TS', hue: 211 },
  tsx: { label: 'TSX', hue: 211 },
  jsx: { label: 'JSX', hue: 50 },
  vue: { label: 'VUE', hue: 145 },
  html: { label: 'HTM', hue: 14 },
  htm: { label: 'HTM', hue: 14 },
  css: { label: 'CSS', hue: 200 },
  scss: { label: 'SCS', hue: 326 },
  java: { label: 'JV', hue: 14 },
  go: { label: 'GO', hue: 187 },
  rs: { label: 'RS', hue: 22 },
  rb: { label: 'RB', hue: 0 },
  php: { label: 'PHP', hue: 240 },
  c: { label: 'C', hue: 220 },
  cpp: { label: 'C++', hue: 220 },
  h: { label: 'H', hue: 220 },
  cs: { label: 'CS', hue: 270 },
  sh: { label: 'SH', hue: 120 },
  bash: { label: 'SH', hue: 120 },
  ps1: { label: 'PS1', hue: 215 },
  bat: { label: 'BAT', hue: 120 },

  // data / serialization
  json: { label: 'JSO', hue: 45 },
  jsonl: { label: 'JSL', hue: 45 },
  yaml: { label: 'YML', hue: 0 },
  yml: { label: 'YML', hue: 0 },
  toml: { label: 'TML', hue: 25 },
  xml: { label: 'XML', hue: 14 },
  csv: { label: 'CSV', hue: 110 },
  tsv: { label: 'TSV', hue: 110 },
  sql: { label: 'SQL', hue: 200 },
  har: { label: 'HAR', hue: 280 },
  parquet: { label: 'PRQ', hue: 240 },

  // text / docs
  md: { label: 'MD', hue: 200 },
  markdown: { label: 'MD', hue: 200 },
  txt: { label: 'TXT', hue: 220 },
  log: { label: 'LOG', hue: 0 },
  rst: { label: 'RST', hue: 200 },
  pdf: { label: 'PDF', hue: 0 },
  doc: { label: 'DOC', hue: 213 },
  docx: { label: 'DOC', hue: 213 },
  rtf: { label: 'RTF', hue: 213 },

  // images
  png: { label: 'PNG', hue: 280 },
  jpg: { label: 'JPG', hue: 280 },
  jpeg: { label: 'JPG', hue: 280 },
  gif: { label: 'GIF', hue: 290 },
  webp: { label: 'WBP', hue: 280 },
  svg: { label: 'SVG', hue: 30 },
  bmp: { label: 'BMP', hue: 280 },
  ico: { label: 'ICO', hue: 280 },

  // media
  mp4: { label: 'MP4', hue: 320 },
  mov: { label: 'MOV', hue: 320 },
  webm: { label: 'WBM', hue: 320 },
  mp3: { label: 'MP3', hue: 175 },
  wav: { label: 'WAV', hue: 175 },
  ogg: { label: 'OGG', hue: 175 },
  flac: { label: 'FLC', hue: 175 },

  // archives / binaries
  zip: { label: 'ZIP', hue: 35 },
  tar: { label: 'TAR', hue: 35 },
  gz: { label: 'GZ', hue: 35 },
  '7z': { label: '7Z', hue: 35 },
  rar: { label: 'RAR', hue: 35 },
  exe: { label: 'EXE', hue: 0 },
  dll: { label: 'DLL', hue: 0 },
  so: { label: 'SO', hue: 0 },
  wasm: { label: 'WASM', hue: 240 },
  bin: { label: 'BIN', hue: 0 },
})

const DIR_ICON = Object.freeze({ label: 'DIR', hue: 210, dir: true })
const FALLBACK = Object.freeze({ label: 'FILE', hue: 220 })

function extOf(name) {
  const s = String(name || '').trim()
  if (!s) return ''
  // Take the last extension. Multi-suffix files (foo.min.js) fall back to the
  // tail, which is what `EXT_PRESETS` reasons about anyway.
  const dot = s.lastIndexOf('.')
  if (dot <= 0 || dot === s.length - 1) return ''
  return s.slice(dot + 1).toLowerCase()
}

/**
 * Resolve a chip descriptor for a sandbox item or a free-form filename.
 *
 * The descriptor is **render-only metadata** (label + hue) — actual styling
 * lives in the consumer's CSS. We never return inline styles so themes can
 * override the palette through CSS variables.
 *
 * @param {string|object} input  filename, or sandbox item `{name, kind}`
 * @returns {{label: string, hue: number, dir: boolean}}
 */
export function fileTypeIcon(input) {
  if (input && typeof input === 'object') {
    if (input.kind === 'dir') return DIR_ICON
    const ext = extOf(input.name || input.relative_path || '')
    const preset = ext ? EXT_PRESETS[ext] : null
    return preset ? { ...preset, dir: false } : { ...FALLBACK, dir: false }
  }
  const ext = extOf(input)
  const preset = ext ? EXT_PRESETS[ext] : null
  return preset ? { ...preset, dir: false } : { ...FALLBACK, dir: false }
}

/** Convenience: return the HSL background colour for the chip. */
export function fileTypeChipColor(input) {
  const { hue, dir } = fileTypeIcon(input)
  // Directories: a flat slate-blue. Files: a saturated tint at the preset hue.
  if (dir) return `hsl(${hue}deg 18% 38%)`
  return `hsl(${hue}deg 55% 38%)`
}

// ---------------------------------------------------------------------------
// Sandbox "internal" file classification.
//
// The session sandbox keeps a handful of files that aren't ordinary user
// deliverables. They all stay visible in the file browser (the user may
// want to inspect or reproduce them), but we tag them so it's immediately
// obvious which is which:
//
//   audit  — the AI's self-audit log (audit.jsonl).
//            High noise / low signal for humans: every write_file /
//            read_file / delete event lands here with sha256, size,
//            next_path_hint etc. Read only by `recent_audit()` for the
//            AI's `session_sandbox_status` tool.
//   script — `.tmp/snippet-*.{py,cmd,ps1,sh,bat}`: the actual scripts
//            the AI synthesised and executed via `run_python_snippet`
//            and friends. Useful for the user to inspect "what did the
//            AI actually run?" or to re-run a step manually; this matches
//            backend export policy (snippets are copied under
//            `workspace/verification-scripts/` during bundle export).
//   output — `.tmp/run-output-*.{stdout,stderr}.txt`: captured stdio
//            from those snippet runs.
//   tmp    — anything else under `.tmp/` (rare; safety bucket).
//
// Mirrors the backend split in:
//   - argusai/core/workspace/session_sandbox.py  (AUDIT_FILE, TMP_DIR)
//   - argusai/core/workspace/workspace_export.py (SKIP_FILES + snippet rules)

const INTERNAL_AUDIT_NAMES = new Set(['audit.jsonl'])

const INTERNAL_TMP_PREFIXES = ['.tmp/', '.tmp\\']

const INTERNAL_TMP_SNIPPET_RE = /^(\.tmp[/\\])?snippet-\d+-[a-z0-9]+\.(py|cmd|ps1|sh|bat)$/i

const INTERNAL_TMP_RUN_OUTPUT_RE = /^(\.tmp[/\\])?run-output-\d+-[a-z0-9]+\.(stdout|stderr)\.txt$/i

/**
 * Classify a sandbox item.
 *
 * Returns one of "audit", "script", "output", "tmp", or "" (default —
 * an ordinary user-facing file).
 *
 * Purely a UI hint; nothing about storage or AI access changes.
 */
export function sandboxItemKind(item) {
  if (!item) return ''
  const rel = String(item.relative_path || item.path || '').replace(/^\/+|\/+$/g, '')
  const name = String(item.name || rel.split('/').pop() || '')
  if (INTERNAL_AUDIT_NAMES.has(name) && !rel.includes('/')) return 'audit'
  if (INTERNAL_TMP_SNIPPET_RE.test(rel) || INTERNAL_TMP_SNIPPET_RE.test(name)) return 'script'
  if (INTERNAL_TMP_RUN_OUTPUT_RE.test(rel) || INTERNAL_TMP_RUN_OUTPUT_RE.test(name)) return 'output'
  for (const p of INTERNAL_TMP_PREFIXES) {
    if (rel.startsWith(p)) return 'tmp'
  }
  if (rel === '.tmp' || name === '.tmp') return 'tmp'
  return ''
}

/**
 * Human-readable label + colour tone for the inline badge next to a
 * sandbox row. Returns null for ordinary user files (no badge needed).
 *
 * Tones are CSS classes consumed by `SandboxRow.vue` / `SandboxFlatRow.vue`:
 *   - "accent"   → blue, signals "useful artefact, look here"
 *   - "muted"    → grey, signals "background noise, ignore unless curious"
 *   - "dim"      → darker grey, signals "really not for you"
 */
export function sandboxItemBadge(item) {
  const kind = sandboxItemKind(item)
  switch (kind) {
    case 'audit':
      return {
        label: t('explorer.badgeAudit'),
        tone: 'dim',
        title: t('explorer.badgeAuditTitle'),
      }
    case 'script':
      return {
        label: t('explorer.badgeScript'),
        tone: 'accent',
        title: t('explorer.badgeScriptTitle'),
      }
    case 'output':
      return {
        label: t('explorer.badgeOutput'),
        tone: 'muted',
        title: t('explorer.badgeOutputTitle'),
      }
    case 'tmp':
      return {
        label: t('explorer.badgeTmp'),
        tone: 'muted',
        title: t('explorer.badgeTmpTitle'),
      }
    default:
      return null
  }
}
