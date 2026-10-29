import { watch } from 'vue'
import { route } from '../router.js'
import { api } from '../api.js'
import { i18n, t } from '../i18n.js'
import { theme } from '../theme.js'

function pageLabel(name) {
  return t(`uiContext.page.${name}`, null, name)
}

let publishTimer = null

async function buildPayload(overlays = {}) {
  const name = route.name || 'tasks'
  const params = { ...(route.params || {}) }
  const payload = {
    route: name,
    page_label: pageLabel(name),
    route_params: params,
    task_id: params.id || '',
    raw_hash: route.raw || '',
    overlays: { ...overlays },
    client: {
      theme: {
        preferred: theme.preferred,
        effective: theme.effective,
        color_scheme: theme.effective === 'dark' ? 'dark' : 'light',
      },
    },
  }

  if (name === 'knowledge_entry' && params.id) {
    payload.knowledge_entry_id = params.id
  }
  if (name === 'reverse_job' && params.id) {
    payload.reverse_job_id = params.id
  }

  try {
    if (name === 'mcp') {
      const r = await api.listMcpServers()
      if (r?.ok && Array.isArray(r.servers)) {
        payload.client.mcp_highlight = r.servers.slice(0, 5).map((s) => ({
          name: s.name,
          enabled: s.enabled,
          status: s.last_probe?.status || s.status,
          error: (s.last_probe?.error || s.error || '').slice(0, 120),
        }))
      }
    }
  } catch (_) {
    /* best-effort */
  }

  return payload
}

function schedulePublish(overlays) {
  if (publishTimer) clearTimeout(publishTimer)
  publishTimer = setTimeout(async () => {
    publishTimer = null
    try {
      await api.setUiContext(await buildPayload(overlays))
    } catch (_) {
      /* pywebview not ready */
    }
  }, 80)
}

/**
 * Push live route / overlay state to Python so get_app_snapshot knows the current screen.
 */
export function useUiContextPublisher(getOverlays) {
  watch(
    () => [route.name, route.raw, JSON.stringify(route.params || {}), theme.preferred, theme.effective, i18n.locale],
    () => schedulePublish(typeof getOverlays === 'function' ? getOverlays() : {}),
    { immediate: true },
  )

  if (typeof getOverlays === 'function') {
    watch(getOverlays, () => schedulePublish(getOverlays()), { deep: true })
  }
}
