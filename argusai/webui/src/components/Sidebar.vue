<script setup>
import { computed, onMounted, ref } from 'vue'
import { route, navigate } from '../router.js'
import { t, i18n } from '../i18n.js'
import { state } from '../state.js'
import { theme, setPreferred } from '../theme.js'
import { api } from '../api.js'
import { useSidebarCollapse } from '../composables/useSidebarCollapse.js'
import { useLiveChannel } from '../composables/useLiveChannel.js'
import LocaleSelect from './LocaleSelect.vue'
import argusIcon from '../../../../assets/brand/argusai-icon-Transparency.png'

const { sidebarCollapsed, toggleSidebar } = useSidebarCollapse()
const collapseTitle = computed(() => {
  void i18n.locale
  return sidebarCollapsed.value ? t('nav.expandSidebar') : t('nav.collapseSidebar')
})

const activeName = computed(() => route.name)
const isTasks = computed(() => activeName.value === 'tasks' || activeName.value === 'task' || activeName.value === 'new')
const isAssistantSessions = computed(() => activeName.value === 'assistant_sessions')
const isProxy = computed(() => activeName.value === 'proxy')
const isBrowser = computed(() => activeName.value === 'browser')
const isTeam = computed(() => activeName.value === 'team')

// Aggregate team status for the animated nav indicator (real backend state).
const teamStatus = ref('')
const teamActiveCount = ref(0)
async function loadTeamStatus() {
  try {
    const res = await api.call('list_teams')
    const teams = (res && res.teams) || []
    let active = 0
    let attn = 0
    for (const tm of teams) {
      active += Number(tm.status?.active || 0)
      attn += Number(tm.status?.attention || 0)
    }
    teamActiveCount.value = active + attn
    if (attn > 0) teamStatus.value = 'attention'
    else if (active > 0) teamStatus.value = 'running'
    else teamStatus.value = ''
  } catch (_) {
    teamStatus.value = ''
    teamActiveCount.value = 0
  }
}
useLiveChannel('teams', () => { loadTeamStatus() }, { debounceMs: 300 })
onMounted(loadTeamStatus)
const isMcp = computed(() => activeName.value === 'mcp')
const isLocalAgents = computed(() => activeName.value === 'local_agents')
const isChannels = computed(() => activeName.value === 'channels')
const isUserTodos = computed(() => activeName.value === 'user_todos')
const isCron = computed(() => activeName.value === 'cron')
const isKnowledge = computed(() => activeName.value === 'knowledge' || activeName.value === 'knowledge_entry')
const isReverse = computed(() => activeName.value === 'reverse' || activeName.value === 'reverse_job')
const isSettings = computed(() => activeName.value === 'settings')
const isAbout = computed(() => activeName.value === 'about')
const themeOptions = computed(() => [
  { value: 'auto', title: t('theme.auto') },
  { value: 'light', title: t('theme.light') },
  { value: 'dark', title: t('theme.dark') },
])
const themeSliderClass = computed(() => `pref-${theme.preferred}`)

function go(ev, path) {
  if (ev) ev.preventDefault()
  navigate(path)
}

const showSidebarLocale = computed(() => !state.customTitlebar)

function setTheme(value) {
  setPreferred(value)
}
</script>
<template>
  <aside class="sidebar" :class="{ collapsed: sidebarCollapsed }">
    <div class="brand">
      <div class="logo"><img :src="argusIcon" alt="ArgusAI" /></div>
      <div class="name">
        {{ t('app.product') }}
        <small>{{ t('app.workbench') }}<template v-if="state.version"> · v{{ state.version }}</template></small>
      </div>
      <button
        type="button"
        class="sidebar-collapse-btn"
        :title="collapseTitle"
        :aria-label="collapseTitle"
        :aria-expanded="!sidebarCollapsed"
        @click="toggleSidebar"
      >
        <svg v-if="sidebarCollapsed" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
          <rect x="3" y="4" width="18" height="16" rx="2"/>
          <path d="M9 4v16"/>
          <path d="M13 9l4 3-4 3"/>
        </svg>
        <svg v-else viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
          <rect x="3" y="4" width="18" height="16" rx="2"/>
          <path d="M9 4v16"/>
          <path d="M17 9l-4 3 4 3"/>
        </svg>
      </button>
    </div>
    <nav class="nav">
      <a :class="{ active: isTasks }" :title="t('nav.tasks')" @click="go($event, '/')">
        <span class="icon">
          <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M8 9h8M8 13h5M8 17h8"/></svg>
        </span>
        <span class="nav-label">{{ t('nav.tasks') }}</span>
      </a>
      <a :class="{ active: isAssistantSessions }" :title="t('nav.assistantSessions')" @click="go($event, '/assistant')">
        <span class="icon">
          <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M4 5.5A2.5 2.5 0 0 1 6.5 3h11A2.5 2.5 0 0 1 20 5.5v8a2.5 2.5 0 0 1-2.5 2.5H10l-5 4v-4.5A2.5 2.5 0 0 1 4 13.5z"/>
            <path d="M8 8h8M8 12h5"/>
          </svg>
        </span>
        <span class="nav-label">{{ t('nav.assistantSessions') }}</span>
      </a>
      <a :class="{ active: isProxy }" :title="t('nav.proxy')" @click="go($event, '/proxy')">
        <span class="icon">
          <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="3" width="20" height="6" rx="1"/><rect x="2" y="15" width="20" height="6" rx="1"/><path d="M12 9v6M8 15l4-6 4 6"/></svg>
        </span>
        <span class="nav-label">{{ t('nav.proxy') }}</span>
      </a>
      <a :class="{ active: isBrowser }" :title="t('nav.browser')" @click="go($event, '/browser')">
        <span class="icon">
          <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M3 12h18"/><path d="M12 3a15 15 0 0 1 0 18M12 3a15 15 0 0 0 0 18"/></svg>
        </span>
        <span class="nav-label">{{ t('nav.browser') }}</span>
      </a>
      <a :class="{ active: isTeam }" :title="t('nav.team')" @click="go($event, '/team')">
        <span class="icon">
          <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2"><circle cx="9" cy="8" r="3"/><circle cx="17" cy="9.5" r="2.3"/><path d="M3.5 19a5.5 5.5 0 0 1 11 0"/><path d="M15 19a4 4 0 0 1 6 0"/></svg>
          <span v-if="teamStatus" class="nav-status-dot" :class="teamStatus"></span>
        </span>
        <span class="nav-label">{{ t('nav.team') }}</span>
        <span v-if="teamActiveCount > 0 && !sidebarCollapsed" class="nav-team-badge" :class="teamStatus">
          {{ teamActiveCount }}
        </span>
      </a>
      <a :class="{ active: isMcp }" :title="t('nav.mcp')" @click="go($event, '/mcp')">
        <span class="icon">
          <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2"><path d="M8 8h8v8H8z"/><path d="M4 12h4M16 12h4M12 4v4M12 16v4"/><circle cx="4" cy="12" r="1.5"/><circle cx="20" cy="12" r="1.5"/><circle cx="12" cy="4" r="1.5"/><circle cx="12" cy="20" r="1.5"/></svg>
        </span>
        <span class="nav-label">{{ t('nav.mcp') }}</span>
      </a>
      <a :class="{ active: isLocalAgents }" :title="t('nav.localAgents')" @click="go($event, '/local-agents')">
        <span class="icon">
          <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2"><rect x="4" y="3" width="16" height="18" rx="3"/><path d="M8 8h8M8 12h5"/><circle cx="16.5" cy="16.5" r="1.5"/></svg>
        </span>
        <span class="nav-label">{{ t('nav.localAgents') }}</span>
      </a>
      <a :class="{ active: isChannels }" :title="t('nav.channels')" @click="go($event, '/channels')">
        <span class="icon">
          <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 5.5A2.5 2.5 0 0 1 6.5 3h11A2.5 2.5 0 0 1 20 5.5v8a2.5 2.5 0 0 1-2.5 2.5H10l-5 4v-4.5A2.5 2.5 0 0 1 4 13.5z"/><path d="m8 9 2 2 5-5"/></svg>
        </span>
        <span class="nav-label">{{ t('nav.channels') }}</span>
      </a>
      <a :class="{ active: isUserTodos }" :title="t('nav.userTodos')" @click="go($event, '/user-todos')">
        <span class="icon">
          <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 11l3 3 8-8"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>
        </span>
        <span class="nav-label">{{ t('nav.userTodos') }}</span>
      </a>
      <a :class="{ active: isCron }" :title="t('nav.cron')" @click="go($event, '/cron')">
        <span class="icon">
          <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>
        </span>
        <span class="nav-label">{{ t('nav.cron') }}</span>
      </a>
      <a :class="{ active: isKnowledge }" :title="t('nav.knowledge')" @click="go($event, '/knowledge')">
        <span class="icon">
          <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 4h13a3 3 0 0 1 3 3v13a0 0 0 0 0 0 0H7a3 3 0 0 1-3-3V4z"/><path d="M4 17a3 3 0 0 1 3-3h13"/><path d="M8 7h7"/></svg>
        </span>
        <span class="nav-label">{{ t('nav.knowledge') }}</span>
      </a>
      <a :class="{ active: isReverse }" :title="t('nav.reverse')" @click="go($event, '/reverse')">
        <span class="icon">
          <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 12a9 9 0 0 1 15-6.7L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-15 6.7L3 16"/><path d="M3 21v-5h5"/></svg>
        </span>
        <span class="nav-label">{{ t('nav.reverse') }}</span>
      </a>
      <a :class="{ active: isSettings }" :title="t('nav.settings')" @click="go($event, '/settings')">
        <span class="icon">
          <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
        </span>
        <span class="nav-label">{{ t('nav.settings') }}</span>
      </a>
      <a :class="{ active: isAbout }" :title="t('nav.about')" @click="go($event, '/about')">
        <span class="icon">
          <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2">
            <circle cx="12" cy="12" r="9"/>
            <path d="M12 11v6"/>
            <path d="M12 7.25v.5"/>
          </svg>
        </span>
        <span class="nav-label">{{ t('nav.about') }}</span>
      </a>
    </nav>
    <div v-if="showSidebarLocale" class="sidebar-locale">
      <LocaleSelect variant="sidebar" :collapsed="sidebarCollapsed" />
    </div>
    <div class="sidebar-theme">
      <div
        class="theme-segmented"
        :class="themeSliderClass"
        role="radiogroup"
        :aria-label="t('theme.label')"
      >
        <button
          v-for="opt in themeOptions"
          :key="opt.value"
          type="button"
          class="theme-segment"
          :class="{ active: theme.preferred === opt.value }"
          role="radio"
          :aria-checked="theme.preferred === opt.value"
          :aria-label="opt.title"
          :title="opt.title"
          @click="setTheme(opt.value)"
        >
          <span class="theme-segment-icon" aria-hidden="true">
            <svg v-if="opt.value === 'auto'" viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 3v18"/><path d="M12 3a9 9 0 0 1 0 18z" fill="currentColor" stroke="none"/></svg>
            <svg v-else-if="opt.value === 'light'" viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></svg>
            <svg v-else viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>
          </span>
        </button>
      </div>
    </div>
    <div class="sidebar-attribution">
      <div>{{ t('app.attributionDeveloper') }}</div>
      <div>
        {{ t('app.attributionContact') }}
        <a href="mailto:wer.yiming@gmail.com">wer.yiming@gmail.com</a>
      </div>
    </div>
  </aside>
</template>
