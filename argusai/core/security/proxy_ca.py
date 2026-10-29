"""CA lifecycle for the passive MITM proxy.

Provides stable storage under ``<data_root>/proxy/``, fingerprinting, and
optional OS trust-store installation. We do **not** promise interception of
any specific application (certificate pinning, custom stacks, etc.) — only
that ArgusAI exposes standard MITM prerequisites when the client trusts our
CA and routes HTTP(S) through the local proxy.
"""

from __future__ import annotations

import hashlib
import logging
import platform
import shutil
import subprocess
import time
from pathlib import Path

from argusai.core.infra.win_stdio import quiet_subprocess_kwargs

log = logging.getLogger("argusai.proxy.ca")

CA_CERT_NAME = "ca.pem"
CA_KEY_NAME = "ca.key"
CA_COMMON_NAME = "argusai proxy CA"

# The proxy panel polls status every couple of seconds, and each poll used to
# shell out to certutil/security. Cache the answer so trust probes stay rare;
# install/uninstall drop the entry so the UI reflects them immediately.
_TRUST_CACHE_TTL_SECONDS = 60.0
_trust_cache: dict[str, tuple[float, bool]] = {}


def invalidate_trust_cache() -> None:
    _trust_cache.clear()


def default_proxy_ca_dir(data_root: Path | str) -> Path:
    return Path(data_root).expanduser() / "proxy"


def migrate_legacy_ca_dir(target: Path, *, legacy_dirs: list[Path] | None = None) -> None:
    """Copy CA material from older CWD-based ``proxy_ca/`` if the new dir is empty."""
    target.mkdir(parents=True, exist_ok=True)
    cert = target / CA_CERT_NAME
    if cert.is_file():
        return
    candidates = list(legacy_dirs or [])
    candidates.append(Path.cwd() / "proxy_ca")
    for legacy in candidates:
        legacy_cert = legacy / CA_CERT_NAME
        legacy_key = legacy / CA_KEY_NAME
        if not legacy_cert.is_file():
            continue
        try:
            shutil.copy2(legacy_cert, cert)
            if legacy_key.is_file():
                shutil.copy2(legacy_key, target / CA_KEY_NAME)
            log.info("Migrated proxy CA from %s to %s", legacy, target)
            return
        except OSError as exc:
            log.warning("Failed to migrate proxy CA from %s: %s", legacy, exc)


def ca_cert_path(ca_dir: Path) -> Path:
    return ca_dir / CA_CERT_NAME


def ca_key_path(ca_dir: Path) -> Path:
    return ca_dir / CA_KEY_NAME


def fingerprint_sha256(cert_path: Path) -> str:
    if not cert_path.is_file():
        return ""
    digest = hashlib.sha256(cert_path.read_bytes()).hexdigest().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


def ca_info(ca_dir: Path) -> dict:
    cert = ca_cert_path(ca_dir)
    return {
        "ca_dir": str(ca_dir),
        "ca_cert_path": str(cert) if cert.is_file() else "",
        "ca_exists": cert.is_file(),
        "fingerprint_sha256": fingerprint_sha256(cert) if cert.is_file() else "",
        "common_name": CA_COMMON_NAME,
        "ca_trusted": is_ca_trusted(cert) if cert.is_file() else False,
    }


def macos_login_keychain() -> Path:
    """User login keychain — never the System keychain (that needs an admin password)."""
    home = Path.home() / "Library" / "Keychains"
    modern = home / "login.keychain-db"
    legacy = home / "login.keychain"
    if modern.exists():
        return modern
    if legacy.exists():
        return legacy
    return modern


def is_ca_trusted(cert_path: Path) -> bool:
    """Best-effort check whether the ArgusAI CA is in the user trust store."""
    if not cert_path.is_file():
        return False
    fp = fingerprint_sha256(cert_path).replace(":", "").lower()
    if not fp:
        return False
    cached = _trust_cache.get(fp)
    if cached is not None and (time.monotonic() - cached[0]) < _TRUST_CACHE_TTL_SECONDS:
        return cached[1]
    sys_name = platform.system().lower()
    trusted = False
    try:
        if sys_name.startswith("win"):
            proc = subprocess.run(
                ["certutil", "-user", "-store", "Root"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                **quiet_subprocess_kwargs(),
            )
            blob = (proc.stdout or "") + (proc.stderr or "")
            trusted = CA_COMMON_NAME in blob or fp in blob.replace(" ", "").lower()
        elif sys_name == "darwin":
            # Presence in a keychain is not trust. ``verify-cert`` checks the
            # user Trust Settings without an authorization prompt.
            proc = subprocess.run(
                ["security", "verify-cert", "-c", str(cert_path)],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            trusted = proc.returncode == 0
    except Exception as exc:
        log.debug("CA trust probe failed: %s", exc)
        return False
    _trust_cache[fp] = (time.monotonic(), trusted)
    return trusted


def install_ca_to_user_store(cert_path: Path) -> dict:
    if not cert_path.is_file():
        return {"ok": False, "error": "CA certificate not found"}
    invalidate_trust_cache()
    sys_name = platform.system().lower()
    try:
        if sys_name.startswith("win"):
            proc = subprocess.run(
                ["certutil", "-user", "-addstore", "Root", str(cert_path)],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
                **quiet_subprocess_kwargs(),
            )
            out = ((proc.stdout or "") + (proc.stderr or "")).strip()
            if proc.returncode != 0 and "already in store" not in out.lower():
                return {"ok": False, "error": out or f"certutil exit {proc.returncode}"}
            return {"ok": True, "message": out or "CA installed to Current User \\ Root"}
        if sys_name == "darwin":
            if is_ca_trusted(cert_path):
                return {"ok": True, "message": "CA already trusted in the login keychain", "already_trusted": True}
            # Do **not** pass ``-d``: that writes admin (system-wide) Trust
            # Settings and macOS will ask for an administrator password every
            # time. User Trust Settings + the login keychain match the Windows
            # Current User \ Root path and at most prompt once for the login
            # keychain password.
            keychain = macos_login_keychain()
            proc = subprocess.run(
                [
                    "security", "add-trusted-cert",
                    "-r", "trustRoot",
                    "-p", "ssl",
                    "-p", "basic",
                    "-k", str(keychain),
                    str(cert_path),
                ],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            out = ((proc.stdout or "") + (proc.stderr or "")).strip()
            if proc.returncode != 0:
                invalidate_trust_cache()
                if is_ca_trusted(cert_path):
                    return {"ok": True, "message": out or "CA already trusted in the login keychain"}
                return {"ok": False, "error": out or f"security exit {proc.returncode}"}
            invalidate_trust_cache()
            return {"ok": True, "message": out or "CA added to login keychain"}
        return {
            "ok": False,
            "error": "Automatic CA install is not supported on this OS; export the certificate and install manually.",
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def uninstall_ca_from_user_store(cert_path: Path) -> dict:
    """Best-effort removal of the ArgusAI CA from the OS trust store.

    Matches the install path: Windows uses ``certutil -user -delstore`` against
    the current user's *Root* store; macOS uses ``security delete-certificate``
    against the login keychain. Linux and other platforms must be uninstalled
    manually, the same as install.
    """
    invalidate_trust_cache()
    sys_name = platform.system().lower()
    fp_sha1 = ""
    if cert_path.is_file():
        try:
            from cryptography import x509  # type: ignore

            cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
            fp_sha1 = cert.fingerprint(hashlib.sha1()).hex().upper()  # noqa: S324
        except Exception as exc:  # pragma: no cover - best effort
            log.debug("CA SHA1 probe failed: %s", exc)
    try:
        if sys_name.startswith("win"):
            # Try a SHA1-based delete first (unambiguous), then fall back to CN.
            attempts: list[list[str]] = []
            if fp_sha1:
                attempts.append(["certutil", "-user", "-delstore", "Root", fp_sha1])
            attempts.append(["certutil", "-user", "-delstore", "Root", CA_COMMON_NAME])
            last_out = ""
            for cmd in attempts:
                proc = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=60, check=False,
                    **quiet_subprocess_kwargs(),
                )
                out = ((proc.stdout or "") + (proc.stderr or "")).strip()
                last_out = out
                lower = out.lower()
                if proc.returncode == 0:
                    return {"ok": True, "message": out or "CA removed from Current User \\ Root"}
                if "cannot find" in lower or "not found" in lower or "0x80092004" in lower:
                    # Already absent — treat as success so the UI clears the trust pill.
                    return {"ok": True, "message": out or "CA was not present in trust store"}
            return {"ok": False, "error": last_out or "certutil delstore failed"}
        if sys_name == "darwin":
            keychain = str(macos_login_keychain())
            attempts = []
            if fp_sha1:
                attempts.append(["security", "delete-certificate", "-Z", fp_sha1, "-k", keychain])
            attempts.append(["security", "delete-certificate", "-c", CA_COMMON_NAME, "-k", keychain])
            # Older builds used ``add-trusted-cert -d`` (admin store). Try a
            # keychain-unspecified delete so leftovers can still be removed
            # without sudo; this may prompt once if the cert is in System.
            if fp_sha1:
                attempts.append(["security", "delete-certificate", "-Z", fp_sha1])
            attempts.append(["security", "delete-certificate", "-c", CA_COMMON_NAME])
            last_out = ""
            for cmd in attempts:
                proc = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=60, check=False,
                )
                out = ((proc.stdout or "") + (proc.stderr or "")).strip()
                last_out = out
                if proc.returncode == 0:
                    return {"ok": True, "message": out or "CA removed from login keychain"}
                lower = out.lower()
                if "could not be found" in lower or "no certificate" in lower:
                    return {"ok": True, "message": out or "CA was not present in keychain"}
            return {"ok": False, "error": last_out or "security delete-certificate failed"}
        return {
            "ok": False,
            "error": "Automatic CA uninstall is not supported on this OS; remove the certificate from your trust store manually.",
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def reveal_ca_dir(ca_dir: Path) -> dict:
    ca_dir.mkdir(parents=True, exist_ok=True)
    try:
        if platform.system().lower().startswith("win"):
            import os
            os.startfile(str(ca_dir))  # noqa: S606
        elif platform.system() == "Darwin":
            subprocess.run(["open", str(ca_dir)], check=False, timeout=10)
        else:
            subprocess.run(["xdg-open", str(ca_dir)], check=False, timeout=10)
        return {"ok": True, "path": str(ca_dir)}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "path": str(ca_dir)}
