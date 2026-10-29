/** Rasterize a live HTML table to PNG and copy it to the clipboard. */

import { canvasToPngBlob, copyPngBlob } from './copyPngBlob.js'

const PAD = 14
const SCALE = 2
const LINE = 1.35
const CELL_PAD_X = 10
const CELL_PAD_Y = 7
const MAX_CANVAS = 8192

export function wrapTableText(ctx, text, maxWidth) {
  const lines = []
  for (const para of String(text || '').split('\n')) {
    if (!para) {
      lines.push('')
      continue
    }
    let current = ''
    for (const ch of para) {
      const trial = current + ch
      if (current && ctx.measureText(trial).width > maxWidth) {
        lines.push(current)
        current = ch
      } else {
        current = trial
      }
    }
    if (current) lines.push(current)
  }
  return lines.length ? lines : ['']
}

function fontSizePx(font) {
  const match = String(font || '').match(/(\d+(?:\.\d+)?)px/)
  return match ? Number(match[1]) : 12
}

function cssColor(el, prop, fallback) {
  if (!el) return fallback
  const value = String(getComputedStyle(el)[prop] || '')
  if (!value || value === 'transparent' || value === 'rgba(0, 0, 0, 0)') return fallback
  return value.split(/,(?![^(]*\))/, 1)[0].trim() || fallback
}

function cssFont(el, fallback) {
  if (!el) return fallback
  const style = getComputedStyle(el)
  const size = style.fontSize || '12px'
  const family = style.fontFamily || 'sans-serif'
  const weight = Number(style.fontWeight) >= 600 ? '650' : (style.fontWeight || '400')
  return `${weight} ${size} ${family}`
}

function readTheme(table) {
  const surface = table.closest('.md-table-frame, .md-lightbox-table, .md-lightbox-shell')
  const th = table.querySelector('th')
  const td = table.querySelector('td')
  const even = table.querySelector('tbody tr:nth-child(even) td')
  const root = document.documentElement
  const panel = cssColor(root, 'backgroundColor', '#1b1f27')
  const frameBg = cssColor(surface, 'backgroundColor', cssColor(table, 'backgroundColor', panel))
  return {
    frameBg,
    headerBg: cssColor(th, 'backgroundColor', frameBg),
    headerFg: cssColor(th, 'color', '#f2f4f7'),
    cellFg: cssColor(td, 'color', cssColor(th, 'color', '#d7dbe3')),
    evenBg: even ? cssColor(even, 'backgroundColor', frameBg) : frameBg,
    oddBg: cssColor(td, 'backgroundColor', frameBg),
    border: cssColor(td, 'borderTopColor', cssColor(th, 'borderTopColor', 'rgba(127,127,127,0.35)')),
    font: cssFont(td, '400 12px "Segoe UI", "PingFang SC", sans-serif'),
    headerFont: cssFont(th, '650 12px "Segoe UI", "PingFang SC", sans-serif'),
  }
}

function cellText(cell) {
  return String(cell?.innerText || '').replace(/\u00a0/g, ' ').trimEnd()
}

function lastColumnReserve(table, col, colCount) {
  if (col !== colCount - 1) return 0
  const actions = table.closest('.md-table-frame')?.querySelector('.md-table-actions')
  return actions ? actions.offsetWidth + 8 : 0
}

export async function tableToPngBlob(table) {
  if (!table || table.tagName !== 'TABLE') {
    throw new Error('no table')
  }
  const rows = [...table.rows]
  if (!rows.length) throw new Error('empty table')
  const colCount = Math.max(...rows.map((row) => row.cells.length), 1)
  const theme = readTheme(table)

  const canvas = document.createElement('canvas')
  const ctx = canvas.getContext('2d')
  if (!ctx) throw new Error('no canvas')

  const colWidths = Array.from({ length: colCount }, (_, col) => {
    const reserve = lastColumnReserve(table, col, colCount)
    const live = Math.max(0, (rows[0]?.cells[col]?.offsetWidth || 0) - reserve)
    const widest = Math.max(
      ...rows.map((row) => {
        const cell = row.cells[col]
        ctx.font = cell?.tagName === 'TH' ? theme.headerFont : theme.font
        return Math.ceil(ctx.measureText(cellText(cell)).width) + CELL_PAD_X * 2
      }),
      48,
    )
    return Math.max(live, widest)
  })

  const prepared = rows.map((row) => {
    const cells = [...Array(colCount)].map((_, col) => {
      const cell = row.cells[col]
      const header = cell?.tagName === 'TH'
      const font = header ? theme.headerFont : theme.font
      ctx.font = font
      const maxText = Math.max(24, colWidths[col] - CELL_PAD_X * 2)
      const lines = wrapTableText(ctx, cellText(cell), maxText)
      const height = Math.max(
        cell?.offsetHeight || 0,
        lines.length * Math.ceil(fontSizePx(font) * LINE) + CELL_PAD_Y * 2,
      )
      return { header, lines, height, font }
    })
    return { cells, height: Math.max(...cells.map((cell) => cell.height), 28) }
  })

  const innerW = colWidths.reduce((sum, width) => sum + width, 0)
  const innerH = prepared.reduce((sum, row) => sum + row.height, 0)
  const width = innerW + PAD * 2
  const height = innerH + PAD * 2
  const scale = Math.min(SCALE, MAX_CANVAS / Math.max(width, 1), MAX_CANVAS / Math.max(height, 1))
  canvas.width = Math.max(1, Math.ceil(width * scale))
  canvas.height = Math.max(1, Math.ceil(height * scale))
  ctx.setTransform(scale, 0, 0, scale, 0, 0)

  roundRect(ctx, 0, 0, width, height, 10)
  ctx.fillStyle = theme.frameBg
  ctx.fill()
  ctx.save()
  ctx.clip()

  let y = PAD
  prepared.forEach((row, rowIdx) => {
    let x = PAD
    row.cells.forEach((cell, col) => {
      const w = colWidths[col]
      const h = row.height
      if (cell.header) ctx.fillStyle = theme.headerBg
      else if (rowIdx > 0 && rowIdx % 2 === 0) ctx.fillStyle = theme.evenBg
      else ctx.fillStyle = theme.oddBg
      ctx.fillRect(x, y, w, h)
      ctx.strokeStyle = theme.border
      ctx.lineWidth = 1
      ctx.strokeRect(x + 0.5, y + 0.5, w - 1, h - 1)
      ctx.fillStyle = cell.header ? theme.headerFg : theme.cellFg
      ctx.font = cell.font
      ctx.textBaseline = 'top'
      const lh = Math.ceil(fontSizePx(cell.font) * LINE)
      cell.lines.forEach((line, i) => {
        ctx.fillText(line, x + CELL_PAD_X, y + CELL_PAD_Y + i * lh)
      })
      x += w
    })
    y += row.height
  })
  ctx.restore()

  ctx.strokeStyle = theme.border
  ctx.lineWidth = 1
  roundRect(ctx, 0.5, 0.5, width - 1, height - 1, 10)
  ctx.stroke()

  return canvasToPngBlob(canvas)
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

export async function copyTableAsImage(table) {
  return copyPngBlob(await tableToPngBlob(table), 'table.png')
}
