"""Run the real recorder composable with a controlled native permission bridge."""

from pathlib import Path
import shutil
import subprocess

import pytest

SOURCE = Path(__file__).resolve().parents[2] / "argusai/webui/src/composables/useAssistantVoiceInput.js"
NODE = shutil.which("node")


@pytest.mark.skipif(not NODE, reason="Node is required")
def test_recording_waits_for_consent_and_cancellation_does_not_start_capture():
    script = r"""
import { readFileSync } from 'node:fs';
import assert from 'node:assert/strict';
const hooks = [];
globalThis.vue = { ref: value => ({ value }), computed: fn => ({ get value() { return fn(); } }), onUnmounted: fn => hooks.push(fn) };
globalThis.appState = { platform: 'darwin' };
const calls = [];
globalThis.api = { requestMacosPermission: async name => { calls.push(['request', name]); return { ok: false, error_code: 'macos_microphone_denied' }; } };
const track = { stop() { calls.push(['stopTrack']); } };
Object.defineProperty(globalThis, 'navigator', { value: { mediaDevices: { async getUserMedia(options) { calls.push(['capture', options]); return { getTracks: () => [track] }; } } }, configurable: true });
globalThis.MediaRecorder = class { static isTypeSupported() { return true; } start() { this.state = 'recording'; } stop() { this.state = 'inactive'; } };
let source = readFileSync(process.argv[1], 'utf8')
  .replace("import { computed, onUnmounted, ref } from 'vue'", 'const { computed, onUnmounted, ref } = globalThis.vue')
  .replace("import { api } from '../api.js'", 'const { api } = globalThis')
  .replace("import { state as appState } from '../state.js'", 'const { appState } = globalThis');
const { useAssistantVoiceInput } = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
const voice = useAssistantVoiceInput();
let result = await voice.start();
assert.equal(result.error_code, 'macos_microphone_denied');
assert.equal(voice.state.value, 'idle');
assert.equal(calls.filter(c => c[0] === 'capture').length, 0);
let resolve;
api.requestMacosPermission = () => new Promise(r => { resolve = r; });
const pending = voice.start();
assert.equal(voice.state.value, 'requesting');
voice.cancel();
resolve({ ok: true, granted: true });
assert.equal((await pending).error, 'cancelled');
assert.equal(calls.filter(c => c[0] === 'capture').length, 0);
api.requestMacosPermission = async () => ({ ok: true, granted: true });
assert.equal((await voice.start()).ok, true);
assert.equal(voice.state.value, 'recording');
assert.deepEqual(calls.find(c => c[0] === 'capture')[1], { audio: true });
voice.cancel();
assert.equal(calls.filter(c => c[0] === 'stopTrack').length, 1);
appState.platform = 'win32';
api.requestMacosPermission = () => { throw Error('Windows must not call macOS'); };
assert.equal((await voice.start()).ok, true);
voice.cancel();
hooks.forEach(fn => fn());
"""
    result = subprocess.run([NODE, "--input-type=module", "-e", script, str(SOURCE)], text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
