<script setup>
import { computed } from 'vue'
import { t } from '../i18n.js'

const props = defineProps({ finding: { type: Object, required: true } })

const sevClass = computed(() => (props.finding.severity || 'info').toLowerCase())
const cweUrl = computed(() => props.finding.cwe ? `https://cwe.mitre.org/data/definitions/${props.finding.cwe.split('-')[1]}.html` : null)
</script>
<template>
  <div class="finding">
    <div class="head">
      <span>
        <span :class="['sev-dot', sevClass]"></span>
        <span class="title">{{ finding.title }}</span>
      </span>
      <span class="small">{{ finding.id }}</span>
    </div>
    <div class="body">
      <div v-if="finding.url" class="url">{{ finding.url }}</div>
      <p v-if="finding.description">{{ finding.description }}</p>
      <a v-if="cweUrl" :href="cweUrl" target="_blank" rel="noopener" style="font-size:12px">
        {{ t('finding.cwe') }}: {{ finding.cwe }}
      </a>
      <div v-if="finding.evidence && Object.keys(finding.evidence).length">
        <details>
          <summary>{{ t('finding.evidence') }}</summary>
          <pre>{{ JSON.stringify(finding.evidence, null, 2) }}</pre>
        </details>
      </div>
      <div v-if="finding.remediation" class="remediation">
        <strong>{{ t('finding.remediation') }}</strong>{{ finding.remediation }}
      </div>
    </div>
  </div>
</template>
