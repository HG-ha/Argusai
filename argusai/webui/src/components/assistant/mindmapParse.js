/** Detect and lay out mind maps from fenced Agent markdown (no mermaid runtime). */

const NODE_MAX_W = 176
const NODE_PAD_X = 20
const NODE_PAD_Y = 12
const LINE_H = 16
const H_GAP = 52
const V_GAP = 16
const WORLD_PAD = 18

const FONT_STACK = '12px "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif'

let measureCtx = null

function canvasMeasure(text) {
  if (typeof document !== 'undefined') {
    if (!measureCtx) {
      measureCtx = document.createElement('canvas').getContext('2d')
      if (measureCtx) measureCtx.font = FONT_STACK
    }
    if (measureCtx) return measureCtx.measureText(String(text || '')).width
  }
  let w = 0
  for (const ch of String(text || '')) {
    w += /[\u4e00-\u9fff]/.test(ch) ? 12 : 7.15
  }
  return w
}

export function isMindmapSource(lang, body) {
  const l = String(lang || '').trim().toLowerCase()
  if (l === 'mindmap' || l === 'markmap') return true
  const first = String(body || '').split(/\r?\n/).find((line) => line.trim()) || ''
  const head = first.trim()
  if (l === 'mermaid') return /^mindmap\b/i.test(head)
  if (!l && /^mindmap\s*$/i.test(head)) return true
  return false
}

function stripMermaidShape(text) {
  const raw = String(text || '').trim()
  const patterns = [
    /^(?:[A-Za-z0-9_.-]+)?\(\((.+)\)\)$/,
    /^(?:[A-Za-z0-9_.-]+)?\{\{(.+)\}\}$/,
    /^(?:[A-Za-z0-9_.-]+)?\[\[(.+)\]\]$/,
    /^(?:[A-Za-z0-9_.-]+)?\((.+)\)$/,
    /^(?:[A-Za-z0-9_.-]+)?\[(.+)\]$/,
    /^(?:[A-Za-z0-9_.-]+)?\{(.+)\}$/,
    /^"(.+)"$/,
    /^'(.+)'$/,
  ]
  for (const re of patterns) {
    const m = raw.match(re)
    if (m) return String(m[1] || '').trim()
  }
  return raw
}

function cleanLabel(s) {
  let text = String(s || '').trim()
  text = text.replace(/::icon\([^)]*\)/gi, '')
  text = text.replace(/:::[\w-]+/g, '')
  text = text.replace(/<br\s*\/?>/gi, '\n')
  text = stripMermaidShape(text)
  text = text.replace(/\*\*(.+?)\*\*/g, '$1')
  text = text.replace(/`([^`]+)`/g, '$1')
  return text.trim()
}

function indentDepth(ws) {
  return Math.floor(String(ws || '').replace(/\t/g, '  ').length / 2)
}

function collectItems(source) {
  const lines = String(source || '').replace(/\r\n/g, '\n').split('\n')
  const hasHeading = lines.some((line) => /^#{1,6}\s+\S/.test(line))
  const items = []
  let lastHeadingDepth = 0

  for (const raw of lines) {
    if (!raw.trim()) continue
    const trimmed = raw.trim()
    if (trimmed.startsWith('%%')) continue
    if (/^(classDef|class)\b/.test(trimmed)) continue
    if (/^mindmap\b/i.test(trimmed) && items.length === 0) continue

    const heading = raw.match(/^(#{1,6})\s+(.+)$/)
    if (heading) {
      lastHeadingDepth = heading[1].length - 1
      items.push({ depth: lastHeadingDepth, text: cleanLabel(heading[2]) })
      continue
    }

    const list = raw.match(/^(\s*)[-*+]\s+(.+)$/)
    if (list) {
      const extra = indentDepth(list[1])
      const depth = hasHeading ? lastHeadingDepth + 1 + extra : extra
      items.push({ depth, text: cleanLabel(list[2]) })
      continue
    }

    const plain = raw.match(/^(\s*)(\S.*)$/)
    if (!plain) continue
    items.push({ depth: indentDepth(plain[1]), text: cleanLabel(plain[2]) })
  }

  return items.filter((item) => item.text)
}

function buildTree(items) {
  if (!items.length) return null
  const root = { text: items[0].text, children: [] }
  const stack = [{ depth: items[0].depth, node: root }]
  for (let i = 1; i < items.length; i += 1) {
    const item = items[i]
    const node = { text: item.text, children: [] }
    while (stack.length && item.depth <= stack[stack.length - 1].depth) stack.pop()
    if (!stack.length) {
      root.children.push(node)
      stack.push({ depth: item.depth, node })
    } else {
      stack[stack.length - 1].node.children.push(node)
      stack.push({ depth: item.depth, node })
    }
  }
  return root
}

export function parseMindmap(source) {
  return buildTree(collectItems(source))
}

function wrapLines(text, maxWidth, measure) {
  const paragraphs = String(text || '').split('\n')
  const lines = []
  const chars = (s) => Array.from(s)

  for (const paragraph of paragraphs) {
    if (!paragraph) {
      lines.push('')
      continue
    }
    let current = ''
    const tokens = paragraph.split(/(\s+)/)
    const flushLong = (token) => {
      for (const ch of chars(token)) {
        if (current && measure(current + ch) > maxWidth) {
          lines.push(current)
          current = ch
        } else {
          current += ch
        }
      }
    }
    for (const token of tokens) {
      const trial = current + token
      if (current && measure(trial) > maxWidth) {
        if (measure(token) > maxWidth) {
          if (current.trim()) lines.push(current.replace(/\s+$/, ''))
          current = ''
          flushLong(token)
        } else {
          lines.push(current.replace(/\s+$/, ''))
          current = token.replace(/^\s+/, '')
        }
      } else {
        current = trial
      }
    }
    if (current) lines.push(current.replace(/\s+$/, ''))
  }
  return lines.length ? lines : ['']
}

function measureNode(node, measure) {
  const lines = wrapLines(node.text, NODE_MAX_W - NODE_PAD_X, measure)
  const textW = Math.max(...lines.map((line) => measure(line)), 28)
  return {
    text: node.text,
    lines,
    w: Math.min(NODE_MAX_W, Math.max(44, textW + NODE_PAD_X)),
    h: Math.max(30, lines.length * LINE_H + NODE_PAD_Y),
    children: (node.children || []).map((child) => measureNode(child, measure)),
  }
}

function subtreeWeight(node) {
  return 1 + (node.children || []).reduce((sum, child) => sum + subtreeWeight(child), 0)
}

function layoutChildren(parent, dir) {
  const kids = parent.children || []
  if (!kids.length) return { boxes: [], links: [], height: parent.h, extent: 0 }

  const laid = kids.map((kid) => {
    const sub = layoutChildren(kid, dir)
    return { kid, sub, h: Math.max(kid.h, sub.height) }
  })
  const totalH = laid.reduce((sum, item) => sum + item.h, 0) + V_GAP * Math.max(0, laid.length - 1)

  let y = -totalH / 2
  const boxes = []
  const links = []
  let maxExtent = 0

  for (const item of laid) {
    const cy = y + item.h / 2
    const childX = dir * (parent.w / 2 + H_GAP + item.kid.w / 2)
    boxes.push({
      x: childX - item.kid.w / 2,
      y: cy - item.kid.h / 2,
      w: item.kid.w,
      h: item.kid.h,
      lines: item.kid.lines,
      text: item.kid.text,
      depth: (parent.depth || 0) + 1,
      root: false,
    })
    for (const box of item.sub.boxes) {
      boxes.push({ ...box, x: box.x + childX, y: box.y + cy })
    }
    for (const link of item.sub.links) {
      links.push({
        x1: link.x1 + childX,
        y1: link.y1 + cy,
        x2: link.x2 + childX,
        y2: link.y2 + cy,
      })
    }
    links.push({
      x1: dir * (parent.w / 2),
      y1: 0,
      x2: childX - dir * (item.kid.w / 2),
      y2: cy,
    })
    maxExtent = Math.max(maxExtent, Math.abs(childX) + item.kid.w / 2 + (item.sub.extent || 0))
    y += item.h + V_GAP
  }

  return { boxes, links, height: Math.max(parent.h, totalH), extent: maxExtent }
}

function splitSides(children) {
  const left = []
  const right = []
  let leftW = 0
  let rightW = 0
  for (const child of children) {
    const weight = subtreeWeight(child)
    if (rightW <= leftW) {
      right.push(child)
      rightW += weight
    } else {
      left.push(child)
      leftW += weight
    }
  }
  return { left, right }
}

export function layoutMindmap(tree, measure = canvasMeasure) {
  if (!tree || !String(tree.text || '').trim()) {
    return { width: 0, height: 0, boxes: [], links: [] }
  }
  const sized = measureNode(tree, measure)
  sized.depth = 0
  const { left, right } = splitSides(sized.children || [])
  const rightLaid = layoutChildren({ ...sized, children: right, depth: 0 }, 1)
  const leftLaid = layoutChildren({ ...sized, children: left, depth: 0 }, -1)
  const boxes = [
    {
      x: -sized.w / 2,
      y: -sized.h / 2,
      w: sized.w,
      h: sized.h,
      lines: sized.lines,
      text: sized.text,
      depth: 0,
      root: true,
    },
    ...rightLaid.boxes,
    ...leftLaid.boxes,
  ]
  const links = [...rightLaid.links, ...leftLaid.links]

  let minX = Infinity
  let minY = Infinity
  let maxX = -Infinity
  let maxY = -Infinity
  for (const box of boxes) {
    minX = Math.min(minX, box.x)
    minY = Math.min(minY, box.y)
    maxX = Math.max(maxX, box.x + box.w)
    maxY = Math.max(maxY, box.y + box.h)
  }
  minX -= WORLD_PAD
  minY -= WORLD_PAD
  maxX += WORLD_PAD
  maxY += WORLD_PAD
  const dx = -minX
  const dy = -minY
  return {
    width: Math.max(1, maxX - minX),
    height: Math.max(1, maxY - minY),
    boxes: boxes.map((box, i) => ({ ...box, id: i, x: box.x + dx, y: box.y + dy })),
    links: links.map((link) => ({
      x1: link.x1 + dx,
      y1: link.y1 + dy,
      x2: link.x2 + dx,
      y2: link.y2 + dy,
    })),
  }
}

export function linkPath(link) {
  const dx = (link.x2 - link.x1) * 0.55
  return `M ${link.x1} ${link.y1} C ${link.x1 + dx} ${link.y1}, ${link.x2 - dx} ${link.y2}, ${link.x2} ${link.y2}`
}
