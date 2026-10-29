<script setup>
const props = defineProps({ severity: { type: Object, default: () => ({}) } })
</script>
<template>
  <div class="sev-bar">
    <span v-for="sev in ['critical','high','medium','low','info']" :key="sev"
          v-show="severity[sev] > 0"
          :class="['sev-pill', sev]">
      {{ severity[sev] }}
    </span>
  </div>
</template>
