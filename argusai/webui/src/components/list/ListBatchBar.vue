<script setup>
import { t } from '../../i18n.js'

defineProps({
  count: { type: Number, default: 0 },
  total: { type: Number, default: 0 },
})
defineEmits(['select-page', 'select-all', 'clear'])
</script>

<template>
  <div v-if="count > 0" class="list-batch">
    <span class="list-batch-count">
      {{ t('common.selectedCount', { n: count }) }}
      <span v-if="total" class="list-batch-total"> / {{ total }}</span>
    </span>
    <div class="list-batch-actions">
      <button type="button" class="ghost list-batch-btn" @click="$emit('select-page')">
        {{ t('common.selectPage') }}
      </button>
      <button type="button" class="ghost list-batch-btn" @click="$emit('select-all')">
        {{ t('common.selectAll') }}
      </button>
      <button type="button" class="ghost list-batch-btn" @click="$emit('clear')">
        {{ t('common.clearSelection') }}
      </button>
      <slot />
    </div>
  </div>
</template>
