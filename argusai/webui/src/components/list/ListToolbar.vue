<script setup>
import { t } from '../../i18n.js'

defineProps({
  modelValue: { type: String, default: '' },
  placeholder: { type: String, default: '' },
})
defineEmits(['update:modelValue'])
</script>

<template>
  <div class="list-toolbar">
    <input
      class="list-toolbar-search"
      type="search"
      :value="modelValue"
      :placeholder="placeholder || t('common.search')"
      @input="$emit('update:modelValue', $event.target.value)"
    />
    <div v-if="$slots.filters" class="list-toolbar-filters">
      <slot name="filters" />
    </div>
    <div class="list-toolbar-actions">
      <slot />
    </div>
  </div>
</template>
