<script setup>
import { computed } from 'vue'
import { t } from '../../i18n.js'

const props = defineProps({
  agents: { type: Array, default: () => [] },
  details: { type: Object, default: () => ({}) },
  decisionBusy: { type: Object, default: () => ({}) },
  compact: { type: Boolean, default: false },
  // The composer dock already shows a count + summary in its own toolbar;
  // suppress this component's own header there to avoid a redundant nested
  // title bar (two boxed headers stacked on top of each other reads like a
  // markdown callout nested inside another one).
  hideHeader: { type: Boolean, default: false },
})

const emit = defineEmits(['confirm', 'open-agent', 'open-center'])

const activeStates = new Set(['queued', 'running', 'closing'])
const terminalStates = new Set(['completed', 'failed', 'closed'])

function detailFor(agent) {
  const id = String(agent?.agent_id || '')
  return (id && props.details?.[id]) || agent || {}
}

function pendingTools(agent) {
  const tools = agent?.pending_confirmation?.pending_tools
  return Array.isArray(tools) ? tools.filter(item => item && typeof item === 'object') : []
}

function pendingBlocks(agent) {
  const blocks = agent?.pending_confirmation?.pending_blocks
  return Array.isArray(blocks) ? blocks.filter(item => item && typeof item === 'object') : []
}

function visibleHistoryBlocks(agent) {
  const history = detailFor(agent)?.history
  if (!Array.isArray(history)) return []
  return history.flatMap(message => Array.isArray(message?.blocks) ? message.blocks : [])
}

function latestTodoBlock(agent) {
  const blocks = [...pendingBlocks(agent), ...visibleHistoryBlocks(agent)]
  for (let index = blocks.length - 1; index >= 0; index -= 1) {
    if (blocks[index]?.type === 'todo_list') return blocks[index]
  }
  return null
}

function flattenTodos(items, rows = []) {
  for (const item of Array.isArray(items) ? items : []) {
    if (!item || typeof item !== 'object') continue
    rows.push(item)
    flattenTodos(item.children, rows)
  }
  return rows
}

function progressFor(agent) {
  const detail = detailFor(agent)
  const explicit = detail?.progress || agent?.progress
  if (explicit && typeof explicit === 'object') {
    const current = Number(explicit.current)
    const total = Number(explicit.total)
    if (Number.isFinite(current) || Number.isFinite(total)) {
      return {
        current: Number.isFinite(current) ? current : null,
        total: Number.isFinite(total) ? total : null,
        label: String(explicit.label || explicit.status || '').trim(),
        source: 'progress',
      }
    }
  }

  const todoBlock = latestTodoBlock(agent)
  if (todoBlock) {
    const flat = Array.isArray(todoBlock.flat) && todoBlock.flat.length
      ? todoBlock.flat
      : flattenTodos(todoBlock.todos)
    if (flat.length) {
      const completed = flat.filter(todo => todo?.status === 'completed').length
      const current = todoBlock.current && typeof todoBlock.current === 'object'
        ? String(todoBlock.current.title || todoBlock.current.content || todoBlock.current.id || '').trim()
        : ''
      return {
        current: completed,
        total: flat.length,
        label: current,
        source: 'todo',
      }
    }
  }

  return null
}

function unresolvedUserDecision(agent) {
  const blocks = visibleHistoryBlocks(agent)
  for (let index = blocks.length - 1; index >= 0; index -= 1) {
    const block = blocks[index]
    if (!['user_decision', 'user_decision_batch'].includes(block?.type)) continue
    if (block.submitted_response) continue
    return block
  }
  return null
}

function requestKind(block, tool) {
  if (block?.type === 'user_decision' || block?.type === 'user_decision_batch') return 'user_decision'
  if (block?.type === 'command_plan') return 'command'
  if (String(tool?.name || block?.name || '').startsWith('user_todo_')) return 'tool'
  return 'tool'
}

function requestRows(agent) {
  const tools = pendingTools(agent)
  const blocks = pendingBlocks(agent)
  const rows = tools.map((tool, index) => {
    const block = blocks.find(item => String(item?.confirm_id || '') === String(tool?.id || ''))
      || blocks[index]
      || {}
    return { key: String(tool.id || block.confirm_id || `${index}`), tool, block, kind: requestKind(block, tool) }
  })

  blocks.forEach((block, index) => {
    if (block?.type === 'todo_list') return
    const paired = rows.some(row => row.block === block)
    if (!paired && ['confirm', 'command_plan', 'user_decision', 'user_decision_batch'].includes(block?.type)) {
      rows.push({
        key: String(block.id || block.confirm_id || `block-${index}`),
        tool: null,
        block,
        kind: requestKind(block, null),
      })
    }
  })

  if (!rows.some(row => row.kind === 'user_decision')) {
    const decision = unresolvedUserDecision(agent)
    const stopped = String(detailFor(agent)?.stopped_reason || '')
    if (decision && (String(agent?.status || '') === 'paused' || stopped.includes('decision'))) {
      rows.push({
        key: String(decision.id || 'user-decision'),
        tool: null,
        block: decision,
        kind: 'user_decision',
      })
    }
  }
  return rows
}

function statusKind(agent) {
  if (requestRows(agent).length) return 'waiting'
  const status = String(agent?.status || '')
  if (activeStates.has(status)) return 'active'
  if (terminalStates.has(status)) return 'completed'
  return status === 'paused' ? 'paused' : 'unknown'
}

function statusLabel(agent) {
  const kind = statusKind(agent)
  if (kind === 'waiting') return t('subAgentGroup.statusWaiting')
  return {
    queued: t('subAgentGroup.statusQueued'),
    running: t('subAgentGroup.statusRunning'),
    closing: t('subAgentGroup.statusClosing'),
    paused: t('subAgentGroup.statusPaused'),
    completed: t('subAgentGroup.statusCompleted'),
    failed: t('subAgentGroup.statusFailed'),
    closed: t('subAgentGroup.statusClosed'),
  }[agent?.status] || t('subAgentGroup.statusUnknown')
}

function requestTypeLabel(row) {
  return {
    tool: t('subAgentGroup.toolCall'),
    command: t('subAgentGroup.commandPlan'),
    user_decision: 'user_decision',
  }[row.kind] || t('subAgentGroup.unknownType')
}

function valueOrUnknown(value) {
  if (value == null || value === '') return t('subAgentGroup.unknown')
  if (Array.isArray(value)) return value.length ? value.join(', ') : '[]'
  if (typeof value === 'object') {
    try {
      return Object.keys(value).length ? JSON.stringify(value) : '{}'
    } catch (_) {
      return String(value)
    }
  }
  return String(value)
}

function requestName(row) {
  const block = row.block || {}
  const tool = row.tool || {}
  if (row.kind === 'user_decision') return block.question || block.title || t('subAgentGroup.unknown')
  if (row.kind === 'command') {
    return block.command || block.arguments?.command || block.name || tool.name || t('subAgentGroup.unknown')
  }
  return tool.name || block.name || t('subAgentGroup.unknown')
}

function requestArguments(row) {
  const block = row.block || {}
  const tool = row.tool || {}
  if (row.kind === 'user_decision') {
    const questions = Array.isArray(block.questions) ? block.questions : undefined
    return {
      options: Array.isArray(block.options) ? block.options : undefined,
      questions,
      input_required: block.input_required,
    }
  }
  return tool.arguments ?? block.arguments
}

function requestScope(agent, row) {
  const block = row.block || {}
  const args = requestArguments(row)
  return block.scope
    ?? (args && typeof args === 'object' ? (args.scope ?? args.scope_ref ?? args.target_url ?? args.url) : null)
    ?? detailFor(agent)?.scope_ref
}

function requestPermission(agent, row) {
  const block = row.block || {}
  return block.permission_key || block.permission || row.tool?.permission || detailFor(agent)?.permission
}

function requestRisk(row) {
  return row.block?.risk || row.tool?.risk
}

function canAllowSession(agent) {
  const tools = pendingTools(agent)
  if (!tools.length) return false
  const blocks = pendingBlocks(agent)
  return tools.every((tool, index) => {
    const block = blocks.find(item => String(item?.confirm_id || '') === String(tool?.id || ''))
      || blocks[index]
    return Array.isArray(block?.actions)
      && block.actions.some(action => action?.value === 'allow_session')
  })
}

function confirmableRequests(agent) {
  return requestRows(agent).filter(row => row.kind !== 'user_decision')
}

function summaryFor(agent) {
  const detail = detailFor(agent)
  return detail.summary || agent?.summary || detail.status_message || agent?.status_message || t('subAgentGroup.unknown')
}

const totals = computed(() => {
  const counts = { active: 0, waiting: 0, completed: 0, paused: 0, unknown: 0 }
  for (const agent of props.agents) counts[statusKind(agent)] += 1
  return counts
})

// Group by team tag so a spawn_team squad reads as one team instead of a flat
// list of loose threads. Named teams first; ungrouped singles collapse into a
// trailing keyless group with no header.
const teamGroups = computed(() => {
  const map = new Map()
  for (const agent of props.agents) {
    const key = String(agent?.team || '')
    if (!map.has(key)) map.set(key, [])
    map.get(key).push(agent)
  }
  return Array.from(map, ([team, agents]) => ({ team, agents }))
    .sort((a, b) => (a.team ? 0 : 1) - (b.team ? 0 : 1))
})
const hasTeams = computed(() => teamGroups.value.some(group => group.team))

function teamCounts(agents) {
  const counts = { active: 0, waiting: 0, completed: 0, paused: 0, unknown: 0 }
  for (const agent of agents) counts[statusKind(agent)] += 1
  return counts
}

// In the focused single-agent detail pane (compact + exactly one agent) the
// surrounding overlay chrome already identifies the agent, so "子 Agent · 1
// 个线程" reads as noise; show the role directly instead.
const groupTitle = computed(() => {
  if (props.compact && props.agents.length === 1) {
    return props.agents[0]?.role || t('subAgentGroup.subAgent')
  }
  const named = teamGroups.value.filter(group => group.team)
  if (named.length === 1 && named[0].agents.length === props.agents.length) {
    return `${named[0].team} · ${props.agents.length}`
  }
  return t('subAgentGroup.groupTitle', { count: props.agents.length })
})
const groupEyebrow = computed(() => (hasTeams.value ? t('subAgentGroup.teamEyebrow') : t('subAgentGroup.liveGroup')))
</script>

<template>
  <section
    class="subagent-group"
    :class="{ compact }"
    aria-labelledby="subagent-group-title"
  >
    <header v-if="!hideHeader" class="subagent-group-head">
      <div>
        <span class="subagent-eyebrow">{{ groupEyebrow }}</span>
        <h3 id="subagent-group-title">{{ groupTitle }}</h3>
      </div>
      <div class="subagent-group-counts" aria-live="polite">
        <span v-if="totals.active" class="state-chip active">{{ t('subAgentGroup.activeCount', { count: totals.active }) }}</span>
        <span v-if="totals.waiting" class="state-chip waiting">{{ t('subAgentGroup.waitingCount', { count: totals.waiting }) }}</span>
        <span v-if="totals.completed" class="state-chip completed">{{ t('subAgentGroup.completedCount', { count: totals.completed }) }}</span>
      </div>
    </header>

    <div class="subagent-list" role="list" :aria-label="t('subAgentGroup.listAria')">
      <template v-for="group in teamGroups" :key="group.team || '_solo'">
      <div v-if="group.team" class="subagent-team-head">
        <span class="subagent-team-name">{{ group.team }}</span>
        <span class="subagent-team-counts">
          <span v-if="teamCounts(group.agents).active" class="state-chip active">{{ t('subAgentGroup.activeCount', { count: teamCounts(group.agents).active }) }}</span>
          <span v-if="teamCounts(group.agents).waiting" class="state-chip waiting">{{ t('subAgentGroup.waitingCount', { count: teamCounts(group.agents).waiting }) }}</span>
          <span v-if="teamCounts(group.agents).completed" class="state-chip completed">{{ t('subAgentGroup.completedCount', { count: teamCounts(group.agents).completed }) }}</span>
        </span>
      </div>
      <article
        v-for="agent in group.agents"
        :key="agent.agent_id"
        class="subagent-row"
        :class="`is-${statusKind(agent)}`"
        role="listitem"
      >
        <button
          type="button"
          class="subagent-row-main"
          :aria-label="t('subAgentGroup.viewDetailsAria', { role: agent.role || t('subAgentGroup.subAgent') })"
          @click="emit('open-agent', agent)"
        >
          <span class="state-dot" :class="statusKind(agent)" aria-hidden="true"></span>
          <span class="subagent-copy">
            <span class="subagent-role-line">
              <strong>{{ agent.role || t('subAgentGroup.unknownRole') }}</strong>
              <span class="state-text">{{ statusLabel(agent) }}</span>
            </span>
            <span class="subagent-objective">{{ agent.objective || t('subAgentGroup.unknownObjective') }}</span>
            <span class="subagent-summary">{{ summaryFor(agent) }}</span>
          </span>
          <span v-if="progressFor(agent)" class="subagent-progress-copy">
            <span
              v-if="progressFor(agent).total != null && progressFor(agent).total > 0"
              class="progress-track"
              role="progressbar"
              :aria-label="t('subAgentGroup.progressAria', { role: agent.role || t('subAgentGroup.subAgent') })"
              aria-valuemin="0"
              :aria-valuemax="progressFor(agent).total"
              :aria-valuenow="progressFor(agent).current ?? 0"
            >
              <i :style="{ width: `${Math.min(100, Math.max(0, ((progressFor(agent).current ?? 0) / progressFor(agent).total) * 100))}%` }"></i>
            </span>
            <span class="progress-value">
              <template v-if="progressFor(agent).current != null || progressFor(agent).total != null">
                {{ progressFor(agent).current ?? t('subAgentGroup.unknown') }} / {{ progressFor(agent).total ?? t('subAgentGroup.unknown') }}
              </template>
              <template v-else>{{ progressFor(agent).label || t('subAgentGroup.unknown') }}</template>
            </span>
          </span>
        </button>

        <div
          v-if="requestRows(agent).length"
          class="request-stack"
          :aria-label="t('subAgentGroup.pendingRequestsAria', { role: agent.role || t('subAgentGroup.subAgent') })"
        >
          <section
            v-for="row in requestRows(agent)"
            :key="row.key"
            class="request-card"
            :class="`kind-${row.kind}`"
          >
            <div class="request-title">
              <span class="request-kind" :class="`kind-${row.kind}`">{{ requestTypeLabel(row) }}</span>
              <strong>{{ requestName(row) }}</strong>
              <span class="risk-chip" :class="`risk-${String(requestRisk(row) || 'unknown').toLowerCase()}`">
                {{ t('subAgentGroup.riskValue', { risk: valueOrUnknown(requestRisk(row)) }) }}
              </span>
            </div>
            <dl class="request-facts">
              <div><dt>name</dt><dd><code>{{ requestName(row) }}</code></dd></div>
              <div><dt>arguments</dt><dd><code>{{ valueOrUnknown(requestArguments(row)) }}</code></dd></div>
              <div><dt>scope</dt><dd>{{ valueOrUnknown(requestScope(agent, row)) }}</dd></div>
              <div><dt>permission</dt><dd>{{ valueOrUnknown(requestPermission(agent, row)) }}</dd></div>
              <div><dt>risk</dt><dd>{{ valueOrUnknown(requestRisk(row)) }}</dd></div>
            </dl>
          </section>

          <div v-if="confirmableRequests(agent).length" class="request-actions">
            <span class="request-action-note">
              {{ confirmableRequests(agent).length > 1
                ? t('subAgentGroup.actionsApplyTo', { count: confirmableRequests(agent).length })
                : t('subAgentGroup.confirmationVersionNote') }}
            </span>
            <button
              type="button"
              class="request-action danger"
              :disabled="decisionBusy[agent.agent_id]"
              @click="emit('confirm', agent, 'deny')"
            >{{ t('assistant.deny') }}</button>
            <button
              type="button"
              class="request-action allow-once"
              :disabled="decisionBusy[agent.agent_id]"
              @click="emit('confirm', agent, 'allow')"
            >{{ t(confirmableRequests(agent).length > 1 ? 'subAgentGroup.allowAllOnce' : 'subAgentGroup.allowOnce') }}</button>
            <button
              v-if="canAllowSession(agent)"
              type="button"
              class="request-action session"
              :disabled="decisionBusy[agent.agent_id]"
              :title="t('subAgentGroup.allowSessionHint')"
              @click="emit('confirm', agent, 'allow_session')"
            >{{ t('subAgentGroup.allowSession') }}</button>
          </div>
          <div v-else class="request-actions">
            <span class="request-action-note">{{ t('subAgentGroup.decisionRpcUnavailable') }}</span>
          </div>
        </div>
      </article>
      </template>
    </div>

    <footer class="subagent-group-foot">
      <span>{{ t('subAgentGroup.todoHint') }}</span>
      <button type="button" class="open-center" @click="emit('open-center')">{{ t('subAgentGroup.openRunCenter') }}</button>
    </footer>
  </section>
</template>

<style scoped>
.subagent-group {
  width: 100%;
  min-width: 0;
  margin: 10px 0 4px;
  overflow: hidden;
  border: 1px solid color-mix(in srgb, var(--accent) 28%, var(--border));
  border-radius: 14px;
  background: color-mix(in srgb, var(--panel-2) 72%, var(--card-bg));
  color: var(--fg);
}
.subagent-group-head,
.subagent-group-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 10px 12px;
}
.subagent-group-head {
  border-bottom: 1px solid var(--border);
}
.subagent-eyebrow {
  display: block;
  margin-bottom: 2px;
  color: var(--muted);
  font-size: 10px;
  letter-spacing: .08em;
  text-transform: uppercase;
}
.subagent-group h3 {
  margin: 0;
  color: var(--fg-strong);
  font-size: 13px;
}
.subagent-group-counts {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 5px;
}
.state-chip,
.request-kind,
.risk-chip {
  display: inline-flex;
  align-items: center;
  min-height: 20px;
  padding: 2px 7px;
  border-radius: 6px;
  font-size: 10px;
  font-weight: 650;
}
.state-chip.active {
  color: var(--accent);
  background: color-mix(in srgb, var(--accent) 12%, transparent);
}
.state-chip.waiting {
  color: var(--warning, #d99100);
  background: color-mix(in srgb, var(--warning, #d99100) 13%, transparent);
}
.state-chip.completed {
  color: var(--muted);
  background: color-mix(in srgb, var(--fg) 7%, transparent);
}
/* request-kind is colored per operation type so 工具调用 / command_plan /
   user_decision stay visually distinguishable everywhere this card renders. */
.request-kind {
  color: var(--muted);
  background: color-mix(in srgb, var(--fg) 8%, transparent);
}
.request-kind.kind-tool {
  color: var(--accent);
  background: color-mix(in srgb, var(--accent) 14%, transparent);
}
.request-kind.kind-command {
  color: var(--fg-strong);
  background: color-mix(in srgb, var(--fg) 13%, transparent);
}
.request-kind.kind-user_decision {
  color: var(--warning, #d99100);
  background: color-mix(in srgb, var(--warning, #d99100) 16%, transparent);
}
.subagent-list {
  display: flex;
  flex-direction: column;
}
.subagent-team-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 7px 12px;
  background: color-mix(in srgb, var(--accent) 7%, transparent);
  border-top: 1px solid var(--border);
}
.subagent-team-head:first-child {
  border-top: 0;
}
.subagent-team-name {
  color: var(--accent);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: .03em;
}
.subagent-team-counts {
  display: flex;
  gap: 5px;
}
.subagent-row + .subagent-row {
  border-top: 1px solid var(--border);
}
.subagent-row-main {
  display: grid;
  width: 100%;
  min-width: 0;
  grid-template-columns: 8px minmax(0, 1fr) auto;
  gap: 9px;
  align-items: start;
  padding: 10px 12px;
  border: 0;
  background: transparent;
  color: inherit;
  text-align: left;
  cursor: pointer;
}
.subagent-row-main:hover {
  background: color-mix(in srgb, var(--accent) 6%, transparent);
}
.subagent-row-main:focus-visible,
.request-action:focus-visible,
.open-center:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}
.state-dot {
  width: 8px;
  height: 8px;
  margin-top: 5px;
  border-radius: 50%;
  background: var(--muted);
}
.state-dot.active {
  background: var(--accent);
  animation: subagent-pulse 1.5s ease-in-out infinite;
}
.state-dot.waiting,
.state-dot.paused {
  background: var(--warning, #d99100);
}
.state-dot.completed {
  background: var(--muted);
}
.subagent-copy {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 3px;
}
.subagent-role-line {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 7px;
}
.subagent-role-line strong,
.subagent-objective,
.subagent-summary {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.subagent-role-line strong {
  color: var(--fg-strong);
  font-size: 12px;
}
.state-text {
  color: var(--muted);
  font-size: 10px;
}
.subagent-objective {
  font-size: 11.5px;
}
.subagent-summary {
  color: var(--muted);
  font-size: 10.5px;
}
.subagent-progress-copy {
  display: flex;
  width: 92px;
  flex-direction: column;
  gap: 4px;
  align-items: flex-end;
}
.progress-track {
  width: 76px;
  height: 4px;
  overflow: hidden;
  border-radius: 999px;
  background: color-mix(in srgb, var(--fg) 12%, transparent);
}
.progress-track i {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: var(--accent);
}
.progress-value {
  max-width: 100%;
  overflow: hidden;
  color: var(--muted);
  font: 10px/1.2 var(--font-mono, Consolas, monospace);
  text-overflow: ellipsis;
  white-space: nowrap;
}
/* Each pending request is its own flat card (border + thin warn accent on the
   left edge). Earlier this wrapped every card in a second, tinted, rounded
   outer box — a "box inside a box" that read like a markdown admonition/tip
   callout rather than a settings-app status row. One flat card per request,
   indented under its agent row, is enough to say "needs you". */
.request-stack {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin: 0 12px 8px 29px;
}
.request-card {
  min-width: 0;
  padding: 8px 8px 8px 9px;
  border: 1px solid var(--border);
  border-left: 3px solid color-mix(in srgb, var(--warning, #d99100) 55%, var(--border));
  border-radius: 8px;
  background: var(--card-bg);
}
.request-title {
  display: flex;
  min-width: 0;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
}
.request-title strong {
  min-width: 0;
  flex: 1;
  overflow: hidden;
  color: var(--fg-strong);
  font: 11px/1.4 var(--font-mono, Consolas, monospace);
  text-overflow: ellipsis;
  white-space: nowrap;
}
.risk-chip {
  color: var(--muted);
  background: color-mix(in srgb, var(--fg) 7%, transparent);
}
.risk-chip.risk-high,
.risk-chip.risk-critical {
  color: var(--danger, #e45b5b);
  background: color-mix(in srgb, var(--danger, #e45b5b) 11%, transparent);
}
.risk-chip.risk-medium,
.risk-chip.risk-medium-high {
  color: var(--warning, #d99100);
  background: color-mix(in srgb, var(--warning, #d99100) 11%, transparent);
}
.request-facts {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 5px 10px;
  margin: 8px 0 0;
}
.request-facts div {
  display: grid;
  min-width: 0;
  grid-template-columns: 66px minmax(0, 1fr);
  gap: 6px;
}
.request-facts dt {
  color: var(--muted);
  font-size: 10px;
}
.request-facts dd {
  min-width: 0;
  overflow: hidden;
  margin: 0;
  font-size: 10.5px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.request-facts code {
  color: inherit;
  font-size: 10px;
}
.request-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  flex-wrap: wrap;
  gap: 6px;
}
.request-action-note {
  min-width: 0;
  flex: 1;
  color: var(--muted);
  font-size: 10px;
}
.request-action,
.open-center {
  min-height: 27px;
  padding: 0 9px;
  border: 1px solid var(--border);
  border-radius: 7px;
  background: transparent;
  color: var(--fg);
  font-size: 11px;
  cursor: pointer;
}
.request-action:hover,
.open-center:hover {
  background: color-mix(in srgb, var(--fg) 7%, transparent);
}
.request-action.danger {
  color: var(--danger, #e45b5b);
  border-color: color-mix(in srgb, var(--danger, #e45b5b) 35%, var(--border));
}
.request-action.allow-once,
.request-action.session {
  color: var(--warning, #d99100);
  border-color: color-mix(in srgb, var(--warning, #d99100) 38%, var(--border));
}
.request-action:disabled {
  opacity: .5;
  cursor: not-allowed;
}
.subagent-group-foot {
  border-top: 1px solid var(--border);
  color: var(--muted);
  font-size: 10.5px;
}
.open-center {
  flex: 0 0 auto;
  color: var(--accent);
}
.compact {
  margin: 0;
}
/* compact is only ever used for the single-agent focus-layer detail pane
   (see AssistantPanel's assistant-agent-center-detail): the surrounding
   overlay chrome already frames "子 Agent 运行中心" and identifies the agent,
   so the group eyebrow/counts and the "打开运行中心" footer are redundant. */
.compact .subagent-group-head {
  padding: 8px 12px;
}
.compact .subagent-eyebrow,
.compact .subagent-group-counts {
  display: none;
}
.compact .subagent-group-foot {
  display: none;
}
@keyframes subagent-pulse {
  50% { opacity: .35; transform: scale(.82); }
}
@media (max-width: 620px) {
  .subagent-group-head,
  .subagent-group-foot {
    align-items: flex-start;
    flex-direction: column;
  }
  .subagent-group-counts {
    justify-content: flex-start;
  }
  .subagent-row-main {
    grid-template-columns: 8px minmax(0, 1fr);
  }
  .subagent-progress-copy {
    width: 100%;
    grid-column: 2;
    align-items: flex-start;
  }
  .progress-track {
    width: min(180px, 100%);
  }
  .request-stack {
    margin-left: 12px;
  }
  .request-facts {
    grid-template-columns: 1fr;
  }
  .request-action-note {
    flex-basis: 100%;
  }
}
@media (prefers-reduced-motion: reduce) {
  .state-dot.active {
    animation: none;
  }
}
</style>
