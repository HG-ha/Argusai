<script setup>
const props = defineProps({
  value: { type: Number, default: 0 },
  label: { type: String, default: '' },
  compact: { type: Boolean, default: false },
  indeterminate: { type: Boolean, default: false },
})
</script>
<template>
  <div :class="['progress', { compact, indeterminate }]">
    <div class="progress-track">
      <div class="progress-fill" :style="{ width: indeterminate ? undefined : value + '%' }"></div>
    </div>
    <div v-if="label || !indeterminate" class="progress-caption">
      <span>{{ label }}</span>
      <span v-if="!indeterminate">{{ Math.round(value) }}%</span>
    </div>
  </div>
</template>
