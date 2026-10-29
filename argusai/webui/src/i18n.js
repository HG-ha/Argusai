// i18n — catalog + dictionary + persisted locale

import { reactive } from 'vue'

import { fallbackLocale, localeMeta, LOCALES, matchLocaleTag } from './i18nCatalog.js'
import { messages } from './i18nMessages.js'

const STORAGE_KEY = 'argusai.locale'

function detectHostLocale() {
  try {
    const tags = [
      ...(navigator.languages || []),
      navigator.language,
    ]
    for (const raw of tags) {
      const id = matchLocaleTag(raw)
      if (id) return id
    }
  } catch (_) { /* WebView without navigator.languages */ }
  return 'en'
}

function readStoredLocale() {
  try {
    return matchLocaleTag(localStorage.getItem(STORAGE_KEY))
  } catch (_) {
    return null
  }
}

function writeStoredLocale(id) {
  try {
    localStorage.setItem(STORAGE_KEY, id)
  } catch (_) { /* private mode / WebView quota */ }
}

function applyDocumentLang(id) {
  const meta = localeMeta(id)
  try {
    document.documentElement.lang = meta?.bcp47 || id || 'en'
  } catch (_) { /* non-DOM test env */ }
}

export const i18n = reactive({
  locale: readStoredLocale() || detectHostLocale(),
})

applyDocumentLang(i18n.locale)

function get(obj, path) {
  return path.split('.').reduce((o, k) => (o == null ? undefined : o[k]), obj)
}

function interpolate(s, vars) {
  if (!vars) return s
  return s.replace(/\{(\w+)\}/g, (_, k) => (k in vars ? String(vars[k]) : `{${k}}`))
}

export function t(key, vars, defaultVal) {
  const loc = i18n.locale
  const fb = fallbackLocale(loc)
  const value = get(messages[loc], key)
    ?? (fb !== loc ? get(messages[fb], key) : undefined)
    ?? get(messages.en, key)
    ?? defaultVal
    ?? key
  return interpolate(value, vars)
}

export function setLocale(locale) {
  const id = matchLocaleTag(locale) || (localeMeta(locale) ? locale : null)
  if (!id) return
  i18n.locale = id
  writeStoredLocale(id)
  applyDocumentLang(id)
}

export function availableLocales() {
  return LOCALES.map((item) => item.id)
}

export function currentLocaleMeta() {
  return localeMeta(i18n.locale) || localeMeta('en')
}

export { LOCALES, localeMeta, matchLocaleTag }
