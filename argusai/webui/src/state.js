// Reactive app-wide state.

import { reactive } from 'vue'

export const state = reactive({
  tasks: [],            // task metas
  loadingTasks: false,
  checks: [],           // available checks
  version: "",
  platform: "",         // "win32" | "darwin" | etc
  framelessWindow: false,
  customTitlebar: false,       // custom header drawn (Windows frameless OR macOS unified)
  nativeWindowControls: false, // macOS: native traffic lights own min/max/close
  titlebarInsetLeft: 0,        // macOS: left space reserved for the traffic lights
});

// Toast helper — a simple timed message box.
export const toast = reactive({
  message: "",
  kind: "ok",           // ok | error
  visible: false,
});

let toastTimer = null;
export function showToast(message, kind = "ok") {
  toast.message = message;
  toast.kind = kind;
  toast.visible = true;
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    toast.visible = false;
  }, 2500);
}
