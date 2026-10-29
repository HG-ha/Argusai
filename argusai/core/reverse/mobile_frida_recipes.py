"""Pre-baked Frida scripts for mobile reverse workflows.

Recipes are **not** executed automatically — ``mobile_frida_recipe_apply`` saves
the script as a ``frida_script`` artifact and returns guidance to run via
``frida_spawn`` / ``frida_attach`` (user confirmation required).
"""

from __future__ import annotations

from typing import Any

_RECIPES: dict[str, dict[str, Any]] = {
    "android_ssl_pinning_bypass": {
        "id": "android_ssl_pinning_bypass",
        "title": "Android SSL pinning bypass (Java)",
        "description": "Hooks common OkHttp / TrustManager pinning checks. Use on apps you are authorized to test.",
        "coverage": ["M-SSL"],
        "platform": "android",
        "mode": "spawn",
        "tags": ["ssl", "pinning", "android"],
        "script": r"""
Java.perform(function () {
  try {
    var TrustManager = Java.use('javax.net.ssl.X509TrustManager');
    var SSLContext = Java.use('javax.net.ssl.SSLContext');
    var TrustManagerImpl = Java.registerClass({
      name: 'com.argusai.TrustAllManager',
      implements: [TrustManager],
      methods: {
        checkClientTrusted: function (chain, authType) {},
        checkServerTrusted: function (chain, authType) {},
        getAcceptedIssuers: function () { return []; }
      }
    });
    var TrustManagers = [TrustManagerImpl.$new()];
    var SSLContextInit = SSLContext.init.overload('[Ljavax.net.ssl.KeyManager;', '[Ljavax.net.ssl.TrustManager;', 'java.security.SecureRandom');
    SSLContextInit.implementation = function (km, tm, sr) {
      SSLContextInit.call(this, km, TrustManagers, sr);
    };
    console.log('[argusai] SSL TrustManager hook installed');
  } catch (e) {
    console.log('[argusai] ssl bypass error: ' + e);
  }
});
""".strip(),
    },
    "android_root_detect_bypass": {
        "id": "android_root_detect_bypass",
        "title": "Android root detection bypass",
        "description": "Neutralizes common File.exists / Build.TAGS root checks.",
        "coverage": ["M-ROOT"],
        "platform": "android",
        "mode": "spawn",
        "tags": ["root", "anti-debug", "android"],
        "script": r"""
Java.perform(function () {
  try {
    var File = Java.use('java.io.File');
    File.exists.implementation = function () {
      var path = this.getAbsolutePath();
      var blocked = ['su', 'magisk', 'Superuser', '/system/xbin/su'];
      for (var i = 0; i < blocked.length; i++) {
        if (path.indexOf(blocked[i]) >= 0) return false;
      }
      return this.exists.call(this);
    };
    console.log('[argusai] root detect bypass installed');
  } catch (e) {
    console.log('[argusai] root bypass error: ' + e);
  }
});
""".strip(),
    },
    "android_crypto_trace": {
        "id": "android_crypto_trace",
        "title": "Android crypto API trace",
        "description": "Logs Cipher.getInstance / doFinal and MessageDigest.digest calls.",
        "coverage": ["M-CRYPTO"],
        "platform": "android",
        "mode": "spawn",
        "tags": ["crypto", "cipher", "android"],
        "script": r"""
Java.perform(function () {
  try {
    var Cipher = Java.use('javax.crypto.Cipher');
    Cipher.getInstance.overload('java.lang.String').implementation = function (alg) {
      console.log('[cipher.getInstance] ' + alg);
      return this.getInstance.call(this, alg);
    };
    Cipher.doFinal.overload('[B').implementation = function (input) {
      console.log('[cipher.doFinal] in=' + (input ? input.length : 0));
      return this.doFinal.call(this, input);
    };
    var MD = Java.use('java.security.MessageDigest');
    MD.digest.overload('[B').implementation = function (input) {
      console.log('[MessageDigest.digest] algo=' + this.getAlgorithm() + ' in=' + (input ? input.length : 0));
      return this.digest.call(this, input);
    };
    console.log('[argusai] crypto trace installed');
  } catch (e) {
    console.log('[argusai] crypto trace error: ' + e);
  }
});
""".strip(),
    },
    "ios_nsurlsession_trace": {
        "id": "ios_nsurlsession_trace",
        "title": "iOS NSURLSession request trace",
        "description": "Logs NSURLRequest URL/method (ObjC runtime). Requires jailbroken/device Frida.",
        "coverage": ["M-NET"],
        "platform": "ios",
        "mode": "spawn",
        "tags": ["network", "ios"],
        "script": r"""
if (ObjC.available) {
  try {
    var NSURLRequest = ObjC.classes.NSURLRequest;
    Interceptor.attach(NSURLRequest['- URL'].implementation, {
      onLeave: function (retval) {
        try {
          var url = new ObjC.Object(retval).absoluteString().toString();
          console.log('[NSURLRequest URL] ' + url);
        } catch (e) {}
      }
    });
    console.log('[argusai] NSURLSession trace installed');
  } catch (e) {
    console.log('[argusai] ios trace error: ' + e);
  }
} else {
  console.log('[argusai] ObjC runtime not available');
}
""".strip(),
    },
}


def list_recipes(*, platform: str = "") -> list[dict[str, Any]]:
    plat = str(platform or "").strip().lower()
    out: list[dict[str, Any]] = []
    for rec in _RECIPES.values():
        if plat and str(rec.get("platform") or "") != plat:
            continue
        out.append({
            "id": rec["id"],
            "title": rec["title"],
            "description": rec["description"],
            "coverage": list(rec.get("coverage") or []),
            "platform": rec.get("platform"),
            "mode": rec.get("mode"),
            "tags": list(rec.get("tags") or []),
        })
    return out


def get_recipe(recipe_id: str) -> dict[str, Any] | None:
    key = str(recipe_id or "").strip()
    rec = _RECIPES.get(key)
    return dict(rec) if rec else None
