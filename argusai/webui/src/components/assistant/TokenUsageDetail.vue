<script setup>
// Session (composer ring) and per-reply usage body. Context fill is the
// current conversation vs the active model window; token rows are totals.
import { t } from '../../i18n.js'

defineProps({
  usage: { type: Object, default: null },
  context: { type: Object, default: null },
  emptyText: { type: String, default: '' },
})

function formatTokenCount(value) {
  const n = Number(value || 0)
  if (!Number.isFinite(n) || n <= 0) return '0'
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1).replace(/\.0$/, '')}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1).replace(/\.0$/, '')}K`
  return Math.round(n).toLocaleString()
}

function exactTokenCount(value) {
  return `${Number(value || 0).toLocaleString()} ${t('assistant.tokenUnit')}`
}
</script>

<template>
  <div class="usage-body">
    <section v-if="context && context.window > 0" class="context-summary">
      <div class="context-summary-top">
        <div class="context-primary">
          <span class="usage-eyebrow">{{ t('assistant.contextLastPrompt') }}</span>
          <strong class="context-percent" :class="'tone-' + context.tone">
            {{ context.percentLabel }}
          </strong>
        </div>
        <div class="context-count">
          <strong>{{ context.usedLabel }}</strong>
          <span>/ {{ context.windowLabel }} {{ t('assistant.tokenUnit') }}</span>
        </div>
      </div>
      <div class="usage-bar context-bar" role="img" :aria-label="context.percentLabel">
        <span
          class="context-bar-fill"
          :class="'tone-' + context.tone"
          :style="{ width: Math.min(100, context.percent) + '%' }"
        ></span>
      </div>
      <span class="context-caption">{{ t('assistant.contextEstimateHint') }}</span>
    </section>

    <div v-if="!usage" class="tools-empty">
      {{ emptyText || t('assistant.tokenUsageEmpty') }}
    </div>

    <section v-else class="usage-section">
      <div class="usage-section-head">
        <span>{{ t('assistant.tokenUsageSession') }}</span>
        <small>{{ t('assistant.tokenUsageCalls', { count: usage.calls }) }}</small>
      </div>

      <dl class="usage-pair">
        <div>
          <dt>{{ t('assistant.tokenUsageInput') }}</dt>
          <dd :title="exactTokenCount(usage.input)">{{ formatTokenCount(usage.input) }}</dd>
        </div>
        <div>
          <dt>{{ t('assistant.tokenUsageOutput') }}</dt>
          <dd :title="exactTokenCount(usage.output)">{{ formatTokenCount(usage.output) }}</dd>
        </div>
      </dl>

      <div class="usage-group cache-group">
        <div class="usage-group-head">
          <span>{{ t('assistant.tokenUsageCache') }}</span>
          <strong>{{ usage.hitRateLabel }}</strong>
        </div>
        <div class="usage-bar cache-bar" role="img" :aria-label="usage.hitRateLabel">
          <span class="usage-bar-hit" :style="{ width: usage.hitPct + '%' }"></span>
          <span class="usage-bar-write" :style="{ width: usage.writePct + '%' }"></span>
          <span class="usage-bar-miss" :style="{ width: usage.missPct + '%' }"></span>
        </div>
        <dl class="usage-breakdown">
          <div>
            <dt><i class="usage-dot usage-dot--hit" aria-hidden="true"></i>{{ t('assistant.tokenUsageCacheHit') }}</dt>
            <dd :title="exactTokenCount(usage.hit)">{{ formatTokenCount(usage.hit) }}</dd>
          </div>
          <div>
            <dt><i class="usage-dot usage-dot--miss" aria-hidden="true"></i>{{ t('assistant.tokenUsageCacheMiss') }}</dt>
            <dd :title="exactTokenCount(usage.miss)">{{ formatTokenCount(usage.miss) }}</dd>
          </div>
          <div>
            <dt><i class="usage-dot usage-dot--write" aria-hidden="true"></i>{{ t('assistant.tokenUsageCacheWrite') }}</dt>
            <dd :title="exactTokenCount(usage.write)">{{ formatTokenCount(usage.write) }}</dd>
          </div>
        </dl>
      </div>

      <div class="usage-group output-group">
        <div>
          <span>{{ t('assistant.tokenUsageReasoning') }}</span>
          <strong :title="exactTokenCount(usage.reasoning)">{{ formatTokenCount(usage.reasoning) }}</strong>
        </div>
        <div>
          <span>{{ t('assistant.tokenUsageReply') }}</span>
          <strong :title="exactTokenCount(usage.reply)">{{ formatTokenCount(usage.reply) }}</strong>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.tools-empty {
  padding: 18px 2px 10px;
  color: var(--muted);
  font-size: 11px;
  text-align: center;
}
.usage-body {
  display: flex;
  min-height: 0;
  flex: 1 1 auto;
  flex-direction: column;
  gap: 14px;
  padding: 14px;
  overflow-y: auto;
}
.context-summary {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.context-summary-top {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
}
.context-primary {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 2px;
}
.usage-eyebrow {
  color: var(--muted);
  font-size: 10px;
  font-weight: 600;
  letter-spacing: .02em;
}
.context-percent {
  color: var(--success, #3ba55d);
  font-size: 26px;
  font-weight: 720;
  line-height: 1;
  letter-spacing: -.03em;
  font-variant-numeric: tabular-nums;
}
.context-percent.tone-warn { color: var(--warning, #e6a23c); }
.context-percent.tone-danger { color: var(--danger, #e74c3c); }
.context-count {
  display: flex;
  min-width: 0;
  align-items: baseline;
  justify-content: flex-end;
  gap: 4px;
  color: var(--muted);
  font-size: 10px;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.context-count strong {
  color: var(--fg-strong);
  font-size: 13px;
  font-weight: 650;
}
.context-caption {
  color: var(--muted-2, var(--muted));
  font-size: 9.5px;
}
.usage-section {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding-top: 12px;
  border-top: 1px solid var(--border);
}
.usage-section-head,
.usage-group-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
}
.usage-section-head > span,
.usage-group-head > span {
  color: var(--fg-strong);
  font-size: 11px;
  font-weight: 650;
}
.usage-section-head small {
  color: var(--muted);
  font-size: 9.5px;
}
.usage-pair {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px 20px;
  margin: 0;
}
.usage-pair > div,
.output-group > div {
  display: flex;
  min-width: 0;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
}
.usage-pair dt,
.output-group span {
  color: var(--muted);
  font-size: 11px;
}
.usage-pair dd,
.output-group strong {
  margin: 0;
  overflow: hidden;
  color: var(--fg);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.usage-group {
  padding: 10px;
  border: 1px solid var(--border);
  border-radius: 9px;
}
.usage-group-head strong {
  color: var(--success, #3ba55d);
  font-size: 14px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}
.usage-bar {
  display: flex;
  height: 6px;
  border-radius: 999px;
  overflow: hidden;
  background: color-mix(in srgb, var(--fg) 12%, transparent);
}
.context-bar { display: block; }
.context-bar-fill {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: var(--success, #3ba55d);
}
.context-bar-fill.tone-warn { background: var(--warning, #e6a23c); }
.context-bar-fill.tone-danger { background: var(--danger, #e74c3c); }
.cache-bar { margin-top: 8px; }
.usage-bar-hit { background: var(--success, #3ba55d); }
.usage-bar-write { background: var(--warning, #e6a23c); }
.usage-bar-miss { background: var(--danger, #e74c3c); }
.usage-breakdown {
  display: grid;
  gap: 6px;
  margin: 9px 0 0;
}
.usage-breakdown > div {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.usage-breakdown dt {
  display: inline-flex;
  min-width: 0;
  align-items: center;
  gap: 7px;
  color: var(--muted);
  font-size: 10px;
}
.usage-breakdown dd {
  margin: 0;
  color: var(--fg);
  font-size: 10px;
  font-variant-numeric: tabular-nums;
}
.usage-dot {
  width: 6px;
  height: 6px;
  flex: 0 0 6px;
  border-radius: 50%;
}
.usage-dot--hit { background: var(--success, #3ba55d); }
.usage-dot--miss { background: var(--danger, #e74c3c); }
.usage-dot--write { background: var(--warning, #e6a23c); }
.output-group {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px 20px;
  padding: 2px 0 0;
  border: 0;
}
</style>
