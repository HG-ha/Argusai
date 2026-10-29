/** Rasterize a mind-map layout to PNG using the live view's computed colors. */

import { canvasToPngBlob, copyPngBlob } from './copyPngBlob.js'
import { layoutMindmap, linkPath } from './mindmapParse.js'

const PAD = 16
const SCALE = 2
const MAX_CANVAS = 8192
const LINE_H = 16
const RADIUS = 8

function cssColor(el, prop, fallback) {
  if (!el) return fallback
  const value = String(getComputedStyle(el)[prop] || '')
  if (!value || value === 'none' || value === 'transparent' || value === 'rgba(0, 0, 0, 0)') {
    return fallback
  }
  return value.split(/,(?![^(]*\))/, 1)[0].trim() || fallback
}

function cssVar(el, name, fallback) {
  const value = getComputedStyle(el || document.documentElement).getPropertyValue(name).trim()
  return value || fallback
}

function cssFont(el, fallback) {
  if (!el) return fallback
  const style = getComputedStyle(el)
  const size = style.fontSize || '12px'
  const family = style.fontFamily || 'sans-serif'
  const weight = Number(style.fontWeight) >= 600 ? '650' : (style.fontWeight || '400')
  return `${weight} ${size} ${family}`
}

function readTheme(root) {
  const host = root || document.documentElement
  const link = host.querySelector?.('.mm-link')
  const nodeRect = host.querySelector?.('.mm-node:not(.is-root) rect')
  const nodeText = host.querySelector?.('.mm-node:not(.is-root) text')
  const rootRect = host.querySelector?.('.mm-node.is-root rect')
  const rootText = host.querySelector?.('.mm-node.is-root text')
  const deepRect = host.querySelector?.('.mm-node.depth-2 rect, .mm-node.depth-3 rect')
  const bg = cssColor(host, 'backgroundColor', cssVar(host, '--bg-sunken', '#141820'))
  return {
    bg,
    accent: cssVar(host, '--accent', cssColor(link, 'stroke', '#4e8cff')),
    link: cssColor(link, 'stroke', cssVar(host, '--border-strong', '#5b6574')),
    nodeFill: cssColor(nodeRect, 'fill', cssVar(host, '--panel-2', '#222833')),
    nodeStroke: cssColor(nodeRect, 'stroke', cssVar(host, '--border-strong', '#5b6574')),
    nodeFg: cssColor(nodeText, 'fill', cssVar(host, '--fg', '#d7dbe3')),
    rootFill: cssColor(rootRect, 'fill', cssVar(host, '--panel-2', '#2a3344')),
    rootStroke: cssColor(rootRect, 'stroke', cssVar(host, '--accent', '#4e8cff')),
    rootFg: cssColor(rootText, 'fill', cssVar(host, '--fg-strong', '#f2f4f7')),
    deepFill: cssColor(deepRect, 'fill', cssVar(host, '--bg-sunken', '#141820')),
    font: cssFont(nodeText, '400 12px "Segoe UI", "PingFang SC", sans-serif'),
    rootFont: cssFont(rootText, '650 12px "Segoe UI", "PingFang SC", sans-serif'),
  }
}

function nodeFill(box, theme) {
  if (box.root) return theme.rootFill
  if ((box.depth || 0) >= 2) return theme.deepFill
  return theme.nodeFill
}

function roundRect(ctx, x, y, w, h, r) {
  const radius = Math.min(r, w / 2, h / 2)
  ctx.beginPath()
  ctx.moveTo(x + radius, y)
  ctx.arcTo(x + w, y, x + w, y + h, radius)
  ctx.arcTo(x + w, y + h, x, y + h, radius)
  ctx.arcTo(x, y + h, x, y, radius)
  ctx.arcTo(x, y, x + w, y, radius)
  ctx.closePath()
}

export async function mindmapToPngBlob(tree, rootEl) {
  const layout = layoutMindmap(tree)
  if (!layout.boxes.length) throw new Error('empty mindmap')
  const theme = readTheme(rootEl)
  const width = layout.width + PAD * 2
  const height = layout.height + PAD * 2
  const scale = Math.min(SCALE, MAX_CANVAS / Math.max(width, 1), MAX_CANVAS / Math.max(height, 1))
  const canvas = document.createElement('canvas')
  const ctx = canvas.getContext('2d')
  if (!ctx) throw new Error('no canvas')
  canvas.width = Math.max(1, Math.ceil(width * scale))
  canvas.height = Math.max(1, Math.ceil(height * scale))
  ctx.setTransform(scale, 0, 0, scale, 0, 0)

  roundRect(ctx, 0, 0, width, height, 10)
  ctx.fillStyle = theme.bg
  ctx.fill()
  ctx.save()
  ctx.clip()
  if (theme.accent) {
    const glow = ctx.createRadialGradient(width * 0.16, height * 0.14, 0, width * 0.16, height * 0.14, Math.max(width, height) * 0.42)
    glow.addColorStop(0, theme.accent)
    glow.addColorStop(1, 'rgba(0,0,0,0)')
    ctx.globalAlpha = 0.1
    ctx.fillStyle = glow
    ctx.fillRect(0, 0, width, height)
    ctx.globalAlpha = 1
  }

  ctx.save()
  ctx.translate(PAD, PAD)
  ctx.strokeStyle = theme.link
  ctx.lineWidth = 1.35
  ctx.lineCap = 'round'
  ctx.lineJoin = 'round'
  for (const link of layout.links) {
    ctx.stroke(new Path2D(linkPath(link)))
  }

  for (const box of layout.boxes) {
    roundRect(ctx, box.x, box.y, box.w, box.h, RADIUS)
    ctx.fillStyle = nodeFill(box, theme)
    ctx.fill()
    ctx.strokeStyle = box.root ? theme.rootStroke : theme.nodeStroke
    ctx.lineWidth = 1
    ctx.stroke()
    ctx.fillStyle = box.root ? theme.rootFg : theme.nodeFg
    ctx.font = box.root ? theme.rootFont : theme.font
    ctx.textAlign = 'center'
    ctx.textBaseline = 'alphabetic'
    const startY = box.y + (box.h - box.lines.length * LINE_H) / 2 + 12
    box.lines.forEach((line, i) => {
      ctx.fillText(line, box.x + box.w / 2, startY + i * LINE_H)
    })
  }
  ctx.restore()
  ctx.restore()

  ctx.strokeStyle = cssVar(rootEl, '--border', theme.nodeStroke)
  ctx.lineWidth = 1
  roundRect(ctx, 0.5, 0.5, width - 1, height - 1, 10)
  ctx.stroke()

  return canvasToPngBlob(canvas)
}

export async function copyMindmapAsImage(tree, rootEl) {
  return copyPngBlob(await mindmapToPngBlob(tree, rootEl), 'mindmap.png')
}
