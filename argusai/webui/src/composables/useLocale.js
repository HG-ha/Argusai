import { api } from '../api.js'
import { setLocale } from '../i18n.js'

/** Apply a UI locale locally and tell the Python host. */
export function applyLocale(id) {
  setLocale(id)
  api.setLocale(id).catch(() => {})
}
