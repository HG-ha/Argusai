<script setup>
import { computed } from 'vue'
import { theme } from '../theme.js'
import { t } from '../i18n.js'
import { state } from '../state.js'
import { route } from '../router.js'

const runningCount = computed(() => state.tasks.filter(t => t.status === 'running').length)
const totalCount = computed(() => state.tasks.length)
const themeLabel = computed(() => t('theme.' + theme.preferred))
</script>
<template>
  <div class="statusbar">
    <span v-if="runningCount > 0"><span class="pulse"></span>{{ t('status.runningTasks', { n: runningCount }) }}</span>
    <span v-else><span class="dot"></span>{{ t('status.idle') }}</span>
    <span>{{ t('status.totalTasks', { n: totalCount }) }}</span>
    <span class="spacer"></span>
    <span>{{ t('status.theme') }}: {{ themeLabel }}</span>
    <span v-if="state.version">v{{ state.version }}</span>
  </div>
</template>
