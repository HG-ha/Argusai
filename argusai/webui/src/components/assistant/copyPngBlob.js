/** Shared PNG clipboard / download fallback for table and mindmap snapshots. */

export function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename || 'image.png'
  link.click()
  URL.revokeObjectURL(url)
}

export function canvasToPngBlob(canvas) {
  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => {
      if (blob) resolve(blob)
      else reject(new Error('png encode failed'))
    }, 'image/png')
  })
}

export async function copyPngBlob(blob, filename = 'image.png') {
  if (navigator.clipboard?.write && typeof ClipboardItem !== 'undefined') {
    try {
      await navigator.clipboard.write([new ClipboardItem({ 'image/png': blob })])
      return 'copied'
    } catch {
      /* WebView without image clipboard falls back to a file. */
    }
  }
  downloadBlob(blob, filename)
  return 'downloaded'
}
