import { computed, onUnmounted, ref } from 'vue'

export const MAX_VOICE_RECORD_SECONDS = 120

export function pickRecorderMimeType() {
  if (typeof MediaRecorder === 'undefined') return ''
  const candidates = [
    'audio/webm;codecs=opus',
    'audio/webm',
    'audio/ogg;codecs=opus',
    'audio/ogg',
    'audio/mp4',
  ]
  for (const mime of candidates) {
    try {
      if (MediaRecorder.isTypeSupported(mime)) return mime
    } catch {
      // ignore
    }
  }
  return ''
}

export function formatVoiceDuration(seconds) {
  const sec = Math.max(0, Math.floor(Number(seconds) || 0))
  const mm = Math.floor(sec / 60)
  const ss = sec % 60
  return `${mm}:${String(ss).padStart(2, '0')}`
}

/**
 * Hold-to-toggle microphone capture for assistant voice messages.
 * Click once to start, click again to finish and return a Blob.
 */
// Number of recent volume samples surfaced for the in-pill waveform UI.
// 40 bars × ~50 ms = a 2-second sliding window, enough to feel "live"
// without overpowering the cap of the recording capsule.
const WAVEFORM_BARS = 40

export function useAssistantVoiceInput() {
  const state = ref('idle') // idle | recording | processing
  const elapsedSec = ref(0)
  // Instantaneous RMS level (0..1) for the pulsing mic dot.
  const level = ref(0)
  // Rolling sample window for the in-capsule waveform bars.
  const levels = ref(new Array(WAVEFORM_BARS).fill(0))

  let mediaStream = null
  let mediaRecorder = null
  let chunks = []
  let tickTimer = null
  let chosenMime = ''
  // WebAudio plumbing for the live waveform. Kept null when no recording is
  // active so unmount/cancel stays cheap.
  let audioCtx = null
  let analyser = null
  let analyserSource = null
  let analyserBuffer = null
  let rafId = 0
  let lastSampleAt = 0

  const isSupported = computed(() => {
    if (typeof navigator === 'undefined') return false
    if (!navigator.mediaDevices?.getUserMedia) return false
    return typeof MediaRecorder !== 'undefined'
  })

  const progress = computed(() => {
    const elapsed = Math.max(0, elapsedSec.value)
    return Math.min(1, elapsed / MAX_VOICE_RECORD_SECONDS)
  })

  function clearTickTimer() {
    if (tickTimer) {
      clearInterval(tickTimer)
      tickTimer = null
    }
  }

  function teardownAnalyser() {
    if (rafId) {
      try { cancelAnimationFrame(rafId) } catch (_) { /* ignore */ }
      rafId = 0
    }
    try { analyserSource?.disconnect() } catch (_) { /* ignore */ }
    try { analyser?.disconnect() } catch (_) { /* ignore */ }
    if (audioCtx) {
      try { void audioCtx.close() } catch (_) { /* ignore */ }
    }
    analyser = null
    analyserSource = null
    analyserBuffer = null
    audioCtx = null
    lastSampleAt = 0
    level.value = 0
    levels.value = new Array(WAVEFORM_BARS).fill(0)
  }

  function startAnalyser(stream) {
    const Ctor = typeof window !== 'undefined'
      ? (window.AudioContext || window.webkitAudioContext)
      : null
    if (!Ctor || !stream) return
    try {
      audioCtx = new Ctor()
      analyserSource = audioCtx.createMediaStreamSource(stream)
      analyser = audioCtx.createAnalyser()
      analyser.fftSize = 1024
      analyser.smoothingTimeConstant = 0.5
      analyser.minDecibels = -85
      analyser.maxDecibels = -10
      analyserSource.connect(analyser)
      analyserBuffer = new Uint8Array(analyser.fftSize)
      lastSampleAt = 0
      const tick = (now) => {
        if (!analyser) return
        analyser.getByteTimeDomainData(analyserBuffer)
        let sum = 0
        for (let i = 0; i < analyserBuffer.length; i += 1) {
          const v = (analyserBuffer[i] - 128) / 128
          sum += v * v
        }
        const rms = Math.sqrt(sum / analyserBuffer.length)
        // Speech RMS rarely passes ~0.25, so amplify a bit for visual punch.
        const scaled = Math.min(1, rms * 3.5)
        level.value = scaled
        if (!lastSampleAt || now - lastSampleAt >= 50) {
          const next = levels.value.slice(1)
          next.push(scaled)
          levels.value = next
          lastSampleAt = now
        }
        rafId = requestAnimationFrame(tick)
      }
      rafId = requestAnimationFrame(tick)
    } catch (err) {
      console.warn('[argusai] voice analyser init failed', err)
      teardownAnalyser()
    }
  }

  function stopTracks() {
    if (!mediaStream) return
    for (const track of mediaStream.getTracks()) {
      try {
        track.stop()
      } catch {
        // ignore
      }
    }
    mediaStream = null
  }

  function resetRecorder() {
    mediaRecorder = null
    chunks = []
    chosenMime = ''
  }

  function cancel() {
    clearTickTimer()
    teardownAnalyser()
    if (mediaRecorder && mediaRecorder.state !== 'inactive') {
      try {
        mediaRecorder.onstop = null
        mediaRecorder.stop()
      } catch {
        // ignore
      }
    }
    resetRecorder()
    stopTracks()
    state.value = 'idle'
    elapsedSec.value = 0
  }

  async function start() {
    if (state.value !== 'idle') {
      return { ok: false, error: 'busy' }
    }
    if (!isSupported.value) {
      return { ok: false, error: 'unsupported' }
    }
    chosenMime = pickRecorderMimeType()
    try {
      mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true })
      chunks = []
      const options = chosenMime ? { mimeType: chosenMime } : undefined
      mediaRecorder = new MediaRecorder(mediaStream, options)
      mediaRecorder.ondataavailable = (event) => {
        if (event?.data?.size) chunks.push(event.data)
      }
      mediaRecorder.start(250)
      state.value = 'recording'
      elapsedSec.value = 0
      clearTickTimer()
      startAnalyser(mediaStream)
      tickTimer = setInterval(() => {
        elapsedSec.value += 1
        if (elapsedSec.value >= MAX_VOICE_RECORD_SECONDS) {
          void stop()
        }
      }, 1000)
      return { ok: true }
    } catch (err) {
      cancel()
      return { ok: false, error: String(err?.message || err || 'mic_denied') }
    }
  }

  async function stop() {
    if (state.value !== 'recording' || !mediaRecorder) {
      return { ok: false, error: 'not_recording' }
    }
    state.value = 'processing'
    clearTickTimer()
    teardownAnalyser()
    const durationMs = elapsedSec.value * 1000
    const recorder = mediaRecorder
    const stream = mediaStream
    return new Promise((resolve) => {
      recorder.onstop = async () => {
        const sourceMime = normalizeBlobMime(recorder.mimeType || chosenMime || 'audio/webm')
        const sourceBlob = new Blob(chunks, { type: sourceMime })
        resetRecorder()
        stopTracks()
        state.value = 'idle'
        elapsedSec.value = 0
        if (!sourceBlob.size) {
          resolve({ ok: false, error: 'empty' })
          return
        }
        // Always hand off WAV to the backend. DashScope qwen3-asr-flash and
        // most OpenAI-compatible ASRs only officially document audio/wav +
        // audio/mpeg; sending webm/ogg/opus produces opaque 400s. The cost
        // of decoding here is one AudioContext pass on a <=120s clip.
        let outBlob = sourceBlob
        let outMime = sourceMime
        try {
          const wav = await convertBlobToWav(sourceBlob)
          if (wav) {
            outBlob = wav
            outMime = 'audio/wav'
          }
        } catch (err) {
          console.warn('[argusai] voice WAV transcode failed, falling back to source format', err)
        }
        resolve({
          ok: true,
          blob: outBlob,
          mime: outMime,
          durationMs,
          name: voiceAttachmentName(outMime),
        })
      }
      recorder.onerror = () => {
        cancel()
        resolve({ ok: false, error: 'recorder_failed' })
      }
      try {
        if (recorder.state !== 'inactive') recorder.stop()
      } catch (err) {
        cancel()
        resolve({ ok: false, error: String(err?.message || err) })
      }
      // Stop mic LED promptly; blob assembly happens in onstop.
      if (stream) {
        for (const track of stream.getTracks()) {
          try {
            track.stop()
          } catch {
            // ignore
          }
        }
        mediaStream = null
      }
    })
  }

  onUnmounted(() => {
    cancel()
  })

  return {
    state,
    elapsedSec,
    level,
    levels,
    progress,
    isSupported,
    start,
    stop,
    cancel,
    formatVoiceDuration,
    maxSeconds: MAX_VOICE_RECORD_SECONDS,
  }
}

function normalizeBlobMime(raw) {
  let mime = String(raw || '').toLowerCase().split(';')[0].trim()
  if (!mime.startsWith('audio/')) mime = 'audio/webm'
  if (mime === 'audio/x-wav') mime = 'audio/wav'
  return mime
}

function voiceAttachmentName(mime) {
  const ext = mime.includes('ogg') ? 'ogg'
    : mime.includes('mp4') || mime.includes('m4a') ? 'm4a'
      : mime.includes('mpeg') || mime.includes('mp3') ? 'mp3'
        : mime.includes('wav') ? 'wav'
          : 'webm'
  const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19)
  return `voice-${stamp}.${ext}`
}

// Decode any browser-recorded blob (typically webm/opus) and re-encode it as a
// mono 16-bit PCM WAV at 16 kHz — the sample rate every ASR model accepts and
// the only audio format DashScope qwen3-asr-flash officially documents.
async function convertBlobToWav(blob, targetSampleRate = 16000) {
  if (typeof AudioContext === 'undefined' && typeof window.webkitAudioContext === 'undefined') {
    return null
  }
  const Ctor = window.AudioContext || window.webkitAudioContext
  const arrayBuf = await blob.arrayBuffer()
  let decoded
  const decodeCtx = new Ctor()
  try {
    decoded = await new Promise((resolve, reject) => {
      // decodeAudioData has two signatures; the promise form throws in older
      // WebKits, the callback form is universally supported.
      try {
        const p = decodeCtx.decodeAudioData(arrayBuf.slice(0), resolve, reject)
        if (p && typeof p.then === 'function') p.then(resolve, reject)
      } catch (err) {
        reject(err)
      }
    })
  } finally {
    try { decodeCtx.close() } catch (_) {}
  }
  if (!decoded) return null
  const targetRate = Math.max(8000, Math.min(48000, targetSampleRate))
  const channels = 1 // ASR is mono
  const offlineLen = Math.ceil(decoded.duration * targetRate)
  if (!offlineLen) return null
  const offline = new OfflineAudioContext(channels, offlineLen, targetRate)
  const src = offline.createBufferSource()
  src.buffer = decoded
  src.connect(offline.destination)
  src.start(0)
  const rendered = await offline.startRendering()
  return encodeWavMono16(rendered.getChannelData(0), targetRate)
}

function encodeWavMono16(float32, sampleRate) {
  const len = float32.length
  const bytesPerSample = 2
  const blockAlign = 1 * bytesPerSample
  const byteRate = sampleRate * blockAlign
  const dataSize = len * bytesPerSample
  const buffer = new ArrayBuffer(44 + dataSize)
  const view = new DataView(buffer)
  const writeString = (offset, str) => {
    for (let i = 0; i < str.length; i += 1) view.setUint8(offset + i, str.charCodeAt(i))
  }
  writeString(0, 'RIFF')
  view.setUint32(4, 36 + dataSize, true)
  writeString(8, 'WAVE')
  writeString(12, 'fmt ')
  view.setUint32(16, 16, true)
  view.setUint16(20, 1, true) // PCM
  view.setUint16(22, 1, true) // channels
  view.setUint32(24, sampleRate, true)
  view.setUint32(28, byteRate, true)
  view.setUint16(32, blockAlign, true)
  view.setUint16(34, 16, true) // bits per sample
  writeString(36, 'data')
  view.setUint32(40, dataSize, true)
  let offset = 44
  for (let i = 0; i < len; i += 1) {
    const s = Math.max(-1, Math.min(1, float32[i]))
    view.setInt16(offset, s < 0 ? s * 0x8000 : s * 0x7fff, true)
    offset += 2
  }
  return new Blob([view], { type: 'audio/wav' })
}
