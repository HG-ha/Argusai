/** UI locale catalog. Native names stay visible in the picker in every language. */

export const LOCALES = Object.freeze([
  { id: 'zh', native: '简体中文', english: 'Chinese (Simplified)', bcp47: 'zh-CN' },
  { id: 'zh-Hant', native: '繁體中文', english: 'Chinese (Traditional)', bcp47: 'zh-TW' },
  { id: 'en', native: 'English', english: 'English', bcp47: 'en' },
  { id: 'ja', native: '日本語', english: 'Japanese', bcp47: 'ja' },
  { id: 'ko', native: '한국어', english: 'Korean', bcp47: 'ko' },
  { id: 'de', native: 'Deutsch', english: 'German', bcp47: 'de' },
  { id: 'es', native: 'Español', english: 'Spanish', bcp47: 'es' },
  { id: 'fr', native: 'Français', english: 'French', bcp47: 'fr' },
  { id: 'pt', native: 'Português', english: 'Portuguese', bcp47: 'pt' },
  { id: 'ru', native: 'Русский', english: 'Russian', bcp47: 'ru' },
])

const BY_ID = Object.fromEntries(LOCALES.map((item) => [item.id, item]))

export function localeMeta(id) {
  return BY_ID[id] || null
}

export function fallbackLocale(id) {
  if (id === 'zh-Hant') return 'zh'
  return 'en'
}

/** Map a BCP 47 / OS tag onto a catalog id, or null if unknown. */
export function matchLocaleTag(raw) {
  const tag = String(raw || '').trim().toLowerCase().replace(/_/g, '-')
  if (!tag) return null
  if (tag === 'zh-hant' || tag.startsWith('zh-hant') || tag.startsWith('zh-tw')
    || tag.startsWith('zh-hk') || tag.startsWith('zh-mo') || tag === 'cht') {
    return 'zh-Hant'
  }
  if (tag.startsWith('zh') || tag.startsWith('yue') || tag === 'cn' || tag === 'chs' || tag === 'hans') {
    return 'zh'
  }
  if (BY_ID[tag]) return tag
  const base = tag.split('-')[0]
  if (BY_ID[base]) return base
  return null
}
