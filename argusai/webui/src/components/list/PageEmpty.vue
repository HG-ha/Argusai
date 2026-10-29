<script setup>
defineProps({
  title: { type: String, default: '' },
  description: { type: String, default: '' },
  actionLabel: { type: String, default: '' },
})
defineEmits(['action'])
</script>

<template>
  <div class="page-empty">
    <strong v-if="title">{{ title }}</strong>
    <p v-if="description">{{ description }}</p>
    <slot>
      <button v-if="actionLabel" type="button" class="primary" @click="$emit('action')">
        {{ actionLabel }}
      </button>
    </slot>
  </div>
</template>
