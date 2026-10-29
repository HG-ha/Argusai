<script setup>
import { computed } from 'vue'

const props = defineProps({
  open: { type: Boolean, default: false },
  level: {
    type: String,
    default: 'modal',
    validator: value => ['workspace', 'assistant', 'focus', 'modal', 'media'].includes(value),
  },
  dim: { type: Boolean, default: true },
  closeOnBackdrop: { type: Boolean, default: true },
  teleport: { type: Boolean, default: true },
})

const emit = defineEmits(['close'])
const target = computed(() => (props.teleport ? 'body' : null))

function onBackdrop(event) {
  if (!props.closeOnBackdrop || event.target !== event.currentTarget) return
  emit('close', 'backdrop')
}
</script>

<template>
  <Teleport v-if="target" :to="target">
    <div
      v-if="open"
      class="workbench-overlay"
      :class="[`level-${level}`, { dim }]"
      @pointerdown="onBackdrop"
    >
      <slot />
    </div>
  </Teleport>
  <div
    v-else-if="open"
    class="workbench-overlay"
    :class="[`level-${level}`, { dim }]"
    @pointerdown="onBackdrop"
  >
    <slot />
  </div>
</template>

<style scoped>
.workbench-overlay {
  position: fixed;
  top: var(--titlebar-h, 0);
  right: 0;
  bottom: 0;
  left: 0;
  display: flex;
  min-width: 0;
  min-height: 0;
  align-items: center;
  justify-content: center;
  padding: 24px;
  isolation: isolate;
}
.workbench-overlay.dim {
  background: rgba(4, 7, 12, 0.56);
  backdrop-filter: blur(4px);
}
.level-workspace { z-index: 900; }
.level-assistant { z-index: 1000; }
.level-focus { z-index: 1100; }
.level-modal { z-index: 1200; }
.level-media { z-index: 1300; }

@media (max-width: 640px) {
  .workbench-overlay {
    align-items: stretch;
    padding: 10px;
  }
}

@media (prefers-reduced-transparency: reduce) {
  .workbench-overlay.dim {
    background: rgba(4, 7, 12, 0.78);
    backdrop-filter: none;
  }
}
</style>
