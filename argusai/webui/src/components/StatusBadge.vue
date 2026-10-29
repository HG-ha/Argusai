<script setup>
import { t } from '../i18n.js'
defineProps({ status: String })
</script>
<template>
  <span :class="['badge', status]">{{ t('status.' + status) }}</span>
</template>
