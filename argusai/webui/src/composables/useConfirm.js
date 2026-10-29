import { h } from 'vue'
import { useDialog } from 'naive-ui'
import { t } from '../i18n.js'

const DIALOG_TYPES = new Set(['warning', 'error', 'info', 'success'])
const DESTRUCTIVE_RE = /(?:永久|不可恢复|无法恢复|不能恢复|清空|硬删|彻底删除|删除|卸载|移除证书|purge|hard[\s-]?delete|delete|destroy|uninstall|erase|wipe)/i
const WARNING_RE = /(?:高危|风险|自动允许|自动批准|授权|注入|任意代码|原始 HTTP|迁移|安装运行时|warning|high[\s-]?risk|auto[\s-]?(?:allow|approve)|arbitrary code|inject)/i
const GENERIC_ACTION_RE = /^(?:确定|确认|继续|好|ok|confirm|continue|删除|停止|移动|卸载|清空)$/i
const DETAIL_LABEL_RE = /(?:作用范围|持续时间|始终例外|Scope|Duration|Always exceptions?|Exceptions?)\s*[：:]\s*/gi

function inferDialogType(requestedType, title, content) {
  if (DIALOG_TYPES.has(requestedType)) return requestedType
  const text = `${title}\n${content}`
  if (DESTRUCTIVE_RE.test(text)) return 'error'
  if (WARNING_RE.test(text)) return 'warning'
  return 'info'
}

function actionFromTitle(title, fallback) {
  const cleaned = String(title || '')
    .replace(/[？?！!。.\s]+$/g, '')
    .replace(/^(?:确定|确认|是否|要不要)\s*/i, '')
    .trim()
  if (cleaned && cleaned.length <= 24) return cleaned
  return fallback
}

function explicitActionLabel(positiveText, title, semantic) {
  const supplied = String(positiveText || '').trim()
  if (supplied && !GENERIC_ACTION_RE.test(supplied)) return supplied
  const fallback = semantic === 'error'
    ? t('common.dangerousAction')
    : semantic === 'warning'
      ? t('common.acceptRisk')
      : t('common.confirm')
  return actionFromTitle(title, fallback)
}

function renderInlineTokens(text) {
  return String(text || '')
    .split(/(NO_AUTO_APPROVE(?:_TOOLS)?|NO_AUTO)/g)
    .filter(Boolean)
    .map((part, index) => (
      /^NO_AUTO/.test(part)
        ? h('code', {
            key: index,
            style: {
              padding: '1px 5px',
              borderRadius: '5px',
              background: 'var(--panel-2, rgba(127, 127, 127, 0.12))',
              color: 'var(--fg-strong, currentColor)',
              fontFamily: 'var(--mono, ui-monospace, monospace)',
              fontSize: '0.92em',
            },
          }, part)
        : part
    ))
}

function labeledDetails(text) {
  const source = String(text || '')
  const matches = Array.from(source.matchAll(DETAIL_LABEL_RE))
  if (!matches.length) return null

  const prelude = source.slice(0, matches[0].index).trim()
  const rows = matches.map((match, index) => {
    const start = Number(match.index) + match[0].length
    const end = index + 1 < matches.length ? Number(matches[index + 1].index) : source.length
    return {
      label: String(match[0]).replace(/\s*[：:]\s*$/, ''),
      value: source.slice(start, end).trim(),
    }
  })
  return { prelude, rows }
}

function renderParagraph(text, key) {
  const details = labeledDetails(text)
  if (!details) {
    return h('p', {
      key,
      style: {
        margin: 0,
        whiteSpace: 'pre-wrap',
        overflowWrap: 'anywhere',
      },
    }, renderInlineTokens(text))
  }

  return h('div', { key, style: { display: 'grid', gap: '8px' } }, [
    details.prelude
      ? h('p', {
          style: { margin: 0, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' },
        }, renderInlineTokens(details.prelude))
      : null,
    h('dl', {
      style: {
        display: 'grid',
        gridTemplateColumns: 'minmax(76px, auto) minmax(0, 1fr)',
        gap: '6px 12px',
        margin: 0,
        padding: '10px 12px',
        border: '1px solid var(--border, rgba(127, 127, 127, 0.18))',
        borderRadius: '8px',
        background: 'var(--panel-2, rgba(127, 127, 127, 0.08))',
      },
    }, details.rows.flatMap((row, index) => [
      h('dt', {
        key: `dt-${index}`,
        style: {
          color: 'var(--fg-strong, currentColor)',
          fontWeight: 600,
          whiteSpace: 'nowrap',
        },
      }, row.label),
      h('dd', {
        key: `dd-${index}`,
        style: {
          minWidth: 0,
          margin: 0,
          color: 'var(--muted, currentColor)',
          whiteSpace: 'pre-wrap',
          overflowWrap: 'anywhere',
        },
      }, renderInlineTokens(row.value)),
    ])),
  ])
}

/**
 * Themed replacement for `window.confirm()`.
 *
 * Returns a function that opens a Naive UI dialog and resolves with `true`
 * if the user clicked the positive button, `false` otherwise (negative
 * button, mask click, Esc, close icon). Drop-in usage:
 *
 *   const confirmDialog = useConfirm()
 *   const ok = await confirmDialog({ title, content })
 *   if (!ok) return
 *
 * Multi-line content is rendered with `white-space: pre-line` so `\n` /
 * `\n\n` produce real line breaks instead of collapsing — the existing
 * i18n strings already use `\n\n` for paragraph separators.
 *
 * `type` ∈ {warning, error, info, success} maps to the corresponding
 * dialog variant (icon + accent color). If omitted, permanent actions
 * infer `error`, risky authorization infers `warning`, and neutral
 * confirmations use `info`.
 *
 * MUST be called from a component that is a descendant of
 * `<n-dialog-provider>` (set up once in App.vue).
 */
export function useConfirm() {
  const dialog = useDialog()
  return function confirmDialog(options = {}) {
    const {
      title = '',
      content = '',
      positiveText = t('common.confirm'),
      negativeText = t('common.cancel'),
      type,
    } = options
    return new Promise((resolve) => {
      const text = String(content ?? '')
      const semantic = inferDialogType(type, title, text)
      const variant = dialog[semantic]
      let settled = false
      const settle = (v) => {
        if (settled) return
        settled = true
        resolve(v)
      }
      const paragraphs = text.split(/\n{2,}/).map(part => part.trim()).filter(Boolean)
      const renderContent = () => h('div', {
        style: {
          display: 'grid',
          gap: '10px',
          maxWidth: '520px',
          color: 'var(--fg, inherit)',
          fontSize: '13px',
          lineHeight: 1.6,
        },
      }, (paragraphs.length ? paragraphs : ['']).map(renderParagraph))
      variant.call(dialog, {
        title,
        content: renderContent,
        positiveText: explicitActionLabel(positiveText, title, semantic),
        negativeText,
        positiveButtonProps: {
          type: semantic === 'error' ? 'error' : semantic === 'warning' ? 'warning' : 'primary',
        },
        style: {
          width: 'min(440px, calc(100vw - 24px))',
          maxWidth: '440px',
          borderRadius: '14px',
        },
        actionStyle: {
          gap: '8px',
          flexWrap: 'wrap',
        },
        closable: true,
        maskClosable: true,
        // Do not place keyboard focus on a destructive primary action.
        autoFocus: false,
        onPositiveClick: () => settle(true),
        onNegativeClick: () => settle(false),
        onMaskClick: () => settle(false),
        onClose: () => settle(false),
        onEsc: () => settle(false),
      })
    })
  }
}
