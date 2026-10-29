"""Browser fingerprint profiles + stealth init scripts.

A *profile* is the set of values Playwright will hand to ``new_context``
plus a small JavaScript ``init_script`` that runs before any page script
on every navigation. Together they let the managed browser look like a
"normal" Chrome/Firefox/Safari install instead of the stock
``HeadlessChrome/<version>`` fingerprint Playwright ships with.

We deliberately keep this conservative:

* The renderer is still real Chromium / Firefox / WebKit.  We are not
  trying to win an arms race with commercial anti-bot vendors; we are
  trying to avoid the lowest-effort "is this headless?" checks and to
  give the auditor a reproducible, declared fingerprint to attach to a
  finding.
* Profiles are deterministic by default. ``"random"`` only randomises
  the *choice* of preset; the chosen preset is reported back to the
  caller so a PoC can be replayed by passing the same profile name.
* Per-engine compatibility: ``channel="chrome"`` only applies to
  Chromium; ``is_mobile`` / ``has_touch`` are silently dropped on
  Firefox because Playwright rejects them there.
"""

from __future__ import annotations

import hashlib
import json
import random
from typing import Any


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------


PROFILES: dict[str, dict[str, Any]] = {
    "default": {
        "label": "Playwright default (no overrides)",
        "engine": "chromium",
        "stealth": True,
    },
    "desktop_chrome_win": {
        "label": "Chrome 131 / Windows 11",
        "engine": "chromium",
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "accept_language": "en-US,en;q=0.9",
        "locale": "en-US",
        "timezone_id": "America/New_York",
        "viewport": {"width": 1920, "height": 1080},
        "device_scale_factor": 1.0,
        "platform": "Win32",
        "hardware_concurrency": 12,
        "device_memory": 16,
        "color_scheme": "light",
        "vendor": "Google Inc.",
        "stealth": True,
    },
    "desktop_chrome_mac": {
        "label": "Chrome 131 / macOS 14",
        "engine": "chromium",
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "accept_language": "en-US,en;q=0.9",
        "locale": "en-US",
        "timezone_id": "America/Los_Angeles",
        "viewport": {"width": 1680, "height": 1050},
        "device_scale_factor": 2.0,
        "platform": "MacIntel",
        "hardware_concurrency": 10,
        "device_memory": 16,
        "color_scheme": "light",
        "vendor": "Google Inc.",
        "stealth": True,
    },
    "desktop_chrome_zh": {
        "label": "Chrome 131 / Windows 11 / 中文",
        "engine": "chromium",
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "accept_language": "zh-CN,zh;q=0.9,en;q=0.8",
        "locale": "zh-CN",
        "timezone_id": "Asia/Shanghai",
        "viewport": {"width": 1920, "height": 1080},
        "device_scale_factor": 1.0,
        "platform": "Win32",
        "hardware_concurrency": 8,
        "device_memory": 16,
        "color_scheme": "light",
        "vendor": "Google Inc.",
        "stealth": True,
    },
    "desktop_firefox_linux": {
        "label": "Firefox 133 / Ubuntu",
        "engine": "firefox",
        "user_agent": "Mozilla/5.0 (X11; Linux x86_64; rv:133.0) Gecko/20100101 Firefox/133.0",
        "accept_language": "en-US,en;q=0.5",
        "locale": "en-US",
        "timezone_id": "Europe/Berlin",
        "viewport": {"width": 1680, "height": 1050},
        "device_scale_factor": 1.0,
        "platform": "Linux x86_64",
        "hardware_concurrency": 8,
        "device_memory": 8,
        "color_scheme": "light",
        "vendor": "",
        "stealth": True,
    },
    "desktop_safari_mac": {
        "label": "Safari 18 / macOS 14",
        "engine": "webkit",
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15",
        "accept_language": "en-US,en;q=0.9",
        "locale": "en-US",
        "timezone_id": "America/Los_Angeles",
        "viewport": {"width": 1680, "height": 1050},
        "device_scale_factor": 2.0,
        "platform": "MacIntel",
        "hardware_concurrency": 8,
        "device_memory": 8,
        "color_scheme": "light",
        "vendor": "Apple Computer, Inc.",
        "stealth": True,
    },
    "mobile_iphone_safari": {
        "label": "iPhone 15 Pro Safari",
        "engine": "webkit",
        "user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
        "accept_language": "en-US,en;q=0.9",
        "locale": "en-US",
        "timezone_id": "America/Los_Angeles",
        "viewport": {"width": 393, "height": 852},
        "device_scale_factor": 3.0,
        "is_mobile": True,
        "has_touch": True,
        "platform": "iPhone",
        "hardware_concurrency": 6,
        "device_memory": 6,
        "color_scheme": "light",
        "vendor": "Apple Computer, Inc.",
        "stealth": True,
    },
    "mobile_android_chrome": {
        "label": "Android 14 Chrome 131",
        "engine": "chromium",
        "user_agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36",
        "accept_language": "en-US,en;q=0.9",
        "locale": "en-US",
        "timezone_id": "America/Los_Angeles",
        "viewport": {"width": 412, "height": 915},
        "device_scale_factor": 2.625,
        "is_mobile": True,
        "has_touch": True,
        "platform": "Linux armv8l",
        "hardware_concurrency": 8,
        "device_memory": 8,
        "color_scheme": "light",
        "vendor": "Google Inc.",
        "stealth": True,
    },
}

# Profiles eligible when caller asks for a random preset (not "default",
# not engine-bound presets that may force a wrong engine).
_RANDOM_POOL = [
    "desktop_chrome_win",
    "desktop_chrome_mac",
    "desktop_chrome_zh",
    "mobile_android_chrome",
]


def list_profiles() -> list[dict[str, Any]]:
    """Return a UI-friendly list of available presets."""
    out: list[dict[str, Any]] = []
    for name, prof in PROFILES.items():
        out.append({
            "name": name,
            "label": prof.get("label", name),
            "engine": prof.get("engine", "chromium"),
            "user_agent": prof.get("user_agent", ""),
            "locale": prof.get("locale", ""),
            "timezone_id": prof.get("timezone_id", ""),
            "viewport": prof.get("viewport", {}),
            "is_mobile": bool(prof.get("is_mobile", False)),
            "stealth": bool(prof.get("stealth", True)),
        })
    return out


def resolve_profile(
    profile: str | dict[str, Any] | None,
    *,
    fallback_engine: str = "chromium",
) -> tuple[str, dict[str, Any]]:
    """Resolve a profile spec to ``(resolved_name, profile_dict)``.

    Accepts:
    * ``None`` / ``""`` → ``"default"`` preset (engine = ``fallback_engine``).
    * ``"random"`` → randomly pick one from :data:`_RANDOM_POOL`.
    * ``"<preset name>"`` → look up in :data:`PROFILES`.
    * ``dict`` → treat as an inline override merged on top of ``default``.
    """
    if profile is None or profile == "":
        prof = dict(PROFILES["default"])
        prof.setdefault("engine", fallback_engine)
        return ("default", prof)
    if isinstance(profile, str):
        key = profile.strip().lower()
        if key == "random":
            choice = random.choice(_RANDOM_POOL)
            return (choice, dict(PROFILES[choice]))
        if key in PROFILES:
            return (key, dict(PROFILES[key]))
        # Unknown name → fall back to default.
        return ("default", dict(PROFILES["default"]))
    if isinstance(profile, dict):
        base = dict(PROFILES["default"])
        base.update({k: v for k, v in profile.items() if v not in (None, "")})
        return ("custom", base)
    return ("default", dict(PROFILES["default"]))


def profile_key(profile: dict[str, Any]) -> str:
    """Stable short hash describing the profile, used as BrowserHub cache key."""
    try:
        payload = json.dumps(profile, sort_keys=True, default=str)
    except Exception:
        payload = repr(profile)
    return hashlib.sha1(payload.encode("utf-8", errors="replace")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# new_context kwargs
# ---------------------------------------------------------------------------


# Playwright rejects unknown keys, and some keys are engine-specific.
_CONTEXT_KEYS_ALL = {
    "user_agent",
    "locale",
    "timezone_id",
    "viewport",
    "device_scale_factor",
    "color_scheme",
    "reduced_motion",
    "extra_http_headers",
}
_CONTEXT_KEYS_CHROMIUM_WEBKIT_ONLY = {"is_mobile", "has_touch"}


def automation_window_size() -> tuple[int, int]:
    """Configured default viewport/window for the managed automation browser.

    Reads ``browser_window_{width,height}`` from user settings (default
    1920x1080). Used both as the ``--window-size`` launch flag and as the
    context viewport when the chosen profile does not pin its own.
    """
    try:
        from argusai.core.settings import SettingsManager

        s = SettingsManager().get()
        w = int(getattr(s, "browser_window_width", 1920) or 1920)
        h = int(getattr(s, "browser_window_height", 1080) or 1080)
        return max(320, min(7680, w)), max(240, min(4320, h))
    except Exception:
        return 1920, 1080


def context_kwargs(profile: dict[str, Any], *, engine: str) -> dict[str, Any]:
    """Build the ``new_context(**kwargs)`` argument set for the given engine."""
    out: dict[str, Any] = {}
    for key in _CONTEXT_KEYS_ALL:
        if key in profile and profile[key] not in (None, "", {}):
            out[key] = profile[key]
    if engine in ("chromium", "webkit"):
        for key in _CONTEXT_KEYS_CHROMIUM_WEBKIT_ONLY:
            if key in profile and profile[key] is not None:
                out[key] = profile[key]
    # Accept-Language as extra header so it actually hits the wire even
    # when locale alone doesn't change it (Playwright bug on some
    # versions when accept_language is requested as locale).
    accept_lang = profile.get("accept_language")
    if accept_lang:
        existing = dict(out.get("extra_http_headers") or {})
        existing.setdefault("Accept-Language", accept_lang)
        out["extra_http_headers"] = existing
    return out


# ---------------------------------------------------------------------------
# Stealth init script
# ---------------------------------------------------------------------------


_STEALTH_TEMPLATE = r"""
(() => {
  const PROFILE = __PROFILE_JSON__;
  const safeDefine = (obj, prop, getter) => {
    try { Object.defineProperty(obj, prop, { get: getter, configurable: true }); }
    catch (e) { /* ignore */ }
  };

  // 1. navigator.webdriver -> undefined
  try { safeDefine(Navigator.prototype, 'webdriver', () => undefined); } catch (e) {}

  // 2. window.chrome shim (only when Chromium and missing).
  if (PROFILE.vendor === 'Google Inc.') {
    try {
      if (!window.chrome) {
        window.chrome = { runtime: {} };
      } else if (!window.chrome.runtime) {
        window.chrome.runtime = {};
      }
    } catch (e) {}
  }

  // 3. navigator.languages
  if (PROFILE.languages && PROFILE.languages.length) {
    safeDefine(Navigator.prototype, 'languages', () => PROFILE.languages);
  }

  // 4. navigator.platform / vendor
  if (PROFILE.platform) {
    safeDefine(Navigator.prototype, 'platform', () => PROFILE.platform);
  }
  if (PROFILE.vendor) {
    safeDefine(Navigator.prototype, 'vendor', () => PROFILE.vendor);
  }

  // 5. hardwareConcurrency / deviceMemory
  if (typeof PROFILE.hardwareConcurrency === 'number') {
    safeDefine(Navigator.prototype, 'hardwareConcurrency', () => PROFILE.hardwareConcurrency);
  }
  if (typeof PROFILE.deviceMemory === 'number') {
    safeDefine(Navigator.prototype, 'deviceMemory', () => PROFILE.deviceMemory);
  }

  // 6. navigator.plugins / mimeTypes - real Chrome ships at least these.
  try {
    if (!navigator.plugins || navigator.plugins.length === 0) {
      const fakePlugins = [
        { name: 'PDF Viewer', filename: 'internal-pdf-viewer' },
        { name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer' },
        { name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer' },
        { name: 'Microsoft Edge PDF Viewer', filename: 'internal-pdf-viewer' },
        { name: 'WebKit built-in PDF', filename: 'internal-pdf-viewer' },
      ];
      safeDefine(Navigator.prototype, 'plugins', () => fakePlugins);
      safeDefine(Navigator.prototype, 'mimeTypes', () => [{ type: 'application/pdf', suffixes: 'pdf' }]);
    }
  } catch (e) {}

  // 7. Permissions.query Notification shim - some detectors test that
  // permissions.query({name:'notifications'}) and Notification.permission
  // disagree, which only happens under headless. Realign them.
  try {
    const origQuery = navigator.permissions && navigator.permissions.query
      ? navigator.permissions.query.bind(navigator.permissions)
      : null;
    if (origQuery) {
      navigator.permissions.query = (params) => {
        try {
          if (params && params.name === 'notifications') {
            return Promise.resolve({ state: (window.Notification && Notification.permission) || 'default' });
          }
        } catch (e) {}
        return origQuery(params);
      };
    }
  } catch (e) {}

  // 8. WebGL vendor / renderer mask - return Intel-like strings.
  try {
    const getParameterProxy = (orig) => function (parameter) {
      // UNMASKED_VENDOR_WEBGL = 37445, UNMASKED_RENDERER_WEBGL = 37446
      if (parameter === 37445) return 'Intel Inc.';
      if (parameter === 37446) return 'Intel Iris OpenGL Engine';
      return orig.apply(this, arguments);
    };
    if (window.WebGLRenderingContext) {
      const ogp = WebGLRenderingContext.prototype.getParameter;
      WebGLRenderingContext.prototype.getParameter = getParameterProxy(ogp);
    }
    if (window.WebGL2RenderingContext) {
      const ogp2 = WebGL2RenderingContext.prototype.getParameter;
      WebGL2RenderingContext.prototype.getParameter = getParameterProxy(ogp2);
    }
  } catch (e) {}
})();
"""


def stealth_init_script(profile: dict[str, Any]) -> str:
    """Render the stealth init script with values from ``profile``.

    Returns an empty string when stealth is disabled for the profile.
    """
    if not profile.get("stealth", True):
        return ""
    accept_lang = profile.get("accept_language", "") or profile.get("locale", "")
    langs: list[str] = []
    for entry in (accept_lang or "").split(","):
        tag = entry.split(";", 1)[0].strip()
        if tag and tag not in langs:
            langs.append(tag)
    payload = {
        "languages": langs,
        "platform": profile.get("platform", ""),
        "vendor": profile.get("vendor", ""),
        "hardwareConcurrency": profile.get("hardware_concurrency"),
        "deviceMemory": profile.get("device_memory"),
    }
    try:
        payload_json = json.dumps(payload, ensure_ascii=False)
    except Exception:
        payload_json = "{}"
    return _STEALTH_TEMPLATE.replace("__PROFILE_JSON__", payload_json)


def profile_summary(profile: dict[str, Any]) -> dict[str, Any]:
    """Trimmed view of a profile, safe to embed in tool results."""
    return {
        "label": profile.get("label", ""),
        "engine": profile.get("engine", ""),
        "user_agent": profile.get("user_agent", ""),
        "locale": profile.get("locale", ""),
        "timezone_id": profile.get("timezone_id", ""),
        "viewport": profile.get("viewport", {}),
        "is_mobile": bool(profile.get("is_mobile", False)),
        "stealth": bool(profile.get("stealth", True)),
    }
