/** Keep in sync with argusai/core/reverse/toolchain_settings.py */

export const TOOLCHAIN_PATH_KEYS = [
  'jadx',
  'apktool',
  'frida',
  'adb',
  'zipalign',
  'apksigner',
  'jarsigner',
  'capa',
  'yara',
  'objection',
  'class-dump',
  'ghidra',
  'ida',
  'innoextract',
  'ffmpeg',
]

export function defaultToolchainPaths() {
  return Object.fromEntries(TOOLCHAIN_PATH_KEYS.map((k) => [k, '']))
}

/** Merge saved paths with defaults; preserve unknown future keys from settings.json. */
export function mergeToolchainPaths(existing) {
  const merged = defaultToolchainPaths()
  for (const [k, v] of Object.entries(existing || {})) {
    const key = String(k || '').trim().toLowerCase()
    if (!key) continue
    merged[key] = String(v ?? '')
  }
  return merged
}

export function toolchainPathRows(keys = TOOLCHAIN_PATH_KEYS) {
  const rows = []
  for (let i = 0; i < keys.length; i += 2) {
    rows.push(keys.slice(i, i + 2))
  }
  return rows
}
