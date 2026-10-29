<script setup>
defineProps({
  selected: { type: Boolean, default: false },
  selectable: { type: Boolean, default: true },
  checked: { type: Boolean, default: false },
  disabled: { type: Boolean, default: false },
  ariaLabel: { type: String, default: '' },
})

const emit = defineEmits(['update:checked', 'open'])
</script>

<template>
  <article
    class="workbench-list-row"
    :class="{ selected, disabled }"
    role="listitem"
    @dblclick="$emit('open')"
  >
    <input
      v-if="selectable"
      class="list-row-check"
      type="checkbox"
      :checked="checked"
      :disabled="disabled"
      :aria-label="ariaLabel || undefined"
      @click.stop
      @change="emit('update:checked', $event.target.checked)"
    />
    <div class="workbench-list-row-content">
      <slot />
    </div>
    <div v-if="$slots.actions" class="workbench-list-row-actions">
      <slot name="actions" />
    </div>
  </article>
</template>

<style scoped>
.workbench-list-row {
  display: grid;
  grid-template-columns: 18px minmax(0, 1fr) auto;
  gap: 10px;
  align-items: start;
  min-width: 0;
  padding: 11px 12px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--card-bg);
  transition: border-color 140ms ease, background 140ms ease;
}
.workbench-list-row:hover {
  border-color: var(--border-strong);
}
.workbench-list-row.selected {
  border-color: color-mix(in srgb, var(--accent) 34%, var(--border));
  background: color-mix(in srgb, var(--accent) 8%, var(--card-bg));
}
.workbench-list-row.disabled {
  opacity: 0.66;
}
.workbench-list-row-content {
  min-width: 0;
}
.workbench-list-row-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 6px;
}

@container (max-width: 640px) {
  .workbench-list-row {
    grid-template-columns: 20px minmax(0, 1fr);
  }
  .workbench-list-row-actions {
    grid-column: 2;
    justify-content: flex-start;
    flex-wrap: wrap;
  }
}
</style>
