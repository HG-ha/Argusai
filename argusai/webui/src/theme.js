// Theme management: light / dark / system-follow, persisted to localStorage.
//
// Apply order at boot (early, before Vue mounts, to avoid flash of wrong theme)
// is handled inline in index.html; this module owns the reactive state.

import { reactive, watch } from 'vue'

const STORAGE_KEY = "argusai.theme";
const LEGACY_STORAGE_KEY = "web" + "audit.theme";

// "auto" = follow OS, "light", "dark" = explicit.
function loadPreferred() {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    if (v === "light" || v === "dark" || v === "auto") return v;
    const legacy = localStorage.getItem(LEGACY_STORAGE_KEY);
    if (legacy === "light" || legacy === "dark" || legacy === "auto") {
      localStorage.setItem(STORAGE_KEY, legacy);
      return legacy;
    }
  } catch (_) {}
  return "auto";
}

function systemPrefersDark() {
  try {
    return window.matchMedia("(prefers-color-scheme: dark)").matches;
  } catch (_) {
    return true;
  }
}

export const theme = reactive({
  preferred: loadPreferred(),      // auto | light | dark
  effective: "dark",                // light | dark — what's actually applied
});

function recompute() {
  const pref = theme.preferred;
  const eff = pref === "auto" ? (systemPrefersDark() ? "dark" : "light") : pref;
  theme.effective = eff;
  document.documentElement.setAttribute("data-theme", eff);
}

export function setPreferred(value) {
  theme.preferred = value;
  try { localStorage.setItem(STORAGE_KEY, value); } catch (_) {}
  recompute();
}

export function cyclePreferred() {
  const order = ["auto", "light", "dark"];
  const idx = order.indexOf(theme.preferred);
  setPreferred(order[(idx + 1) % order.length]);
}

// React to OS scheme changes when in auto mode.
try {
  const mq = window.matchMedia("(prefers-color-scheme: dark)");
  const onChange = () => {
    if (theme.preferred === "auto") recompute();
  };
  if (mq.addEventListener) mq.addEventListener("change", onChange);
  else if (mq.addListener) mq.addListener(onChange);
} catch (_) {}

// Initial application.
recompute();

watch(() => theme.preferred, recompute);
