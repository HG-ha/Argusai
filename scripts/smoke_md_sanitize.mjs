// Tiny offline smoke for the URL sanitisers used by MarkdownText.vue.
// Mirrors the implementation in argusai/webui/src/components/assistant/MarkdownText.vue.
// Run: node scripts/smoke_md_sanitize.mjs
function sanitizeImgUrl(rawUrl) {
  const url = String(rawUrl || '').trim()
  if (!url || url.length > 4096) return ''
  if (/[\u0000\r\n]/.test(url)) return ''
  const lower = url.toLowerCase()
  if (lower.startsWith('javascript:') || lower.startsWith('vbscript:') || lower.startsWith('file:')) return ''
  if (lower.startsWith('data:')) {
    return /^data:image\/(png|jpe?g|gif|webp|svg\+xml);/i.test(url) ? url : ''
  }
  if (lower.startsWith('https://')) return url
  if (lower.startsWith('http://')) return url
  return ''
}

function sanitizeLinkUrl(rawUrl) {
  const url = String(rawUrl || '').trim()
  if (!url || url.length > 4096) return ''
  if (/[\u0000\r\n]/.test(url)) return ''
  const lower = url.toLowerCase()
  if (lower.startsWith('https://') || lower.startsWith('http://') || lower.startsWith('mailto:')) return url
  return ''
}

const imgCases = [
  ['https://mdn.alipayobjects.com/one_clip/afts/img/x/original', true],
  ['http://example.com/chart.png', true],
  ['http://127.0.0.1:8080/x.png', true],
  ['data:image/png;base64,iVBORw0KG...', true],
  ['data:text/html;base64,PHNjcmlwdD4=', false],
  ['javascript:alert(1)', false],
  ['JaVaScRiPt:alert(1)', false],
  ['file:///C:/Windows/system32/cmd.exe', false],
  ['  https://ok.com/x.png  ', true],
  ['https://ok.com/x.png\nX-Injected: yes', false],
]
const linkCases = [
  ['https://docs.example.com/', true],
  ['http://localhost/', true],
  ['mailto:foo@bar.com', true],
  ['javascript:alert(1)', false],
  ['data:text/html,<script>...', false],
  ['file:///etc/passwd', false],
  ['ftp://x.y/', false],
]

let bad = 0
for (const [url, shouldPass] of imgCases) {
  const out = sanitizeImgUrl(url)
  const ok = (out !== '') === shouldPass
  console.log((ok ? 'OK ' : 'FAIL') + ` img ${shouldPass ? '+' : '-'} ${url} -> ${out || '<empty>'}`)
  if (!ok) bad++
}
for (const [url, shouldPass] of linkCases) {
  const out = sanitizeLinkUrl(url)
  const ok = (out !== '') === shouldPass
  console.log((ok ? 'OK ' : 'FAIL') + ` link ${shouldPass ? '+' : '-'} ${url} -> ${out || '<empty>'}`)
  if (!ok) bad++
}
if (bad > 0) {
  console.error(`${bad} sanitiser cases failed`)
  process.exit(1)
}
console.log('all sanitiser cases passed')
