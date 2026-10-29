"""Local encrypted credential storage for communication channels."""

from __future__ import annotations

import base64
import ctypes
import json
import os
import sys
import threading
from ctypes import wintypes
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def _dpapi_encrypt(value: bytes) -> bytes:
    buffer = ctypes.create_string_buffer(value)
    incoming = _DataBlob(
        len(value),
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)),
    )
    outgoing = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    protect = crypt32.CryptProtectData
    protect.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    protect.restype = wintypes.BOOL
    if not protect(
        ctypes.byref(incoming),
        "ArgusAI Channel Credential",
        None,
        None,
        None,
        0x01,
        ctypes.byref(outgoing),
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(outgoing.pbData, outgoing.cbData)
    finally:
        local_free = ctypes.windll.kernel32.LocalFree
        local_free.argtypes = [wintypes.HLOCAL]
        local_free.restype = wintypes.HLOCAL
        local_free(ctypes.cast(outgoing.pbData, wintypes.HLOCAL))


def _dpapi_decrypt(value: bytes) -> bytes:
    buffer = ctypes.create_string_buffer(value)
    incoming = _DataBlob(
        len(value),
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)),
    )
    outgoing = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    unprotect = crypt32.CryptUnprotectData
    unprotect.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    unprotect.restype = wintypes.BOOL
    if not unprotect(
        ctypes.byref(incoming),
        None,
        None,
        None,
        None,
        0x01,
        ctypes.byref(outgoing),
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(outgoing.pbData, outgoing.cbData)
    finally:
        local_free = ctypes.windll.kernel32.LocalFree
        local_free.argtypes = [wintypes.HLOCAL]
        local_free.restype = wintypes.HLOCAL
        local_free(ctypes.cast(outgoing.pbData, wintypes.HLOCAL))


class ChannelSecretStore:
    """Encrypted key/value file; values are never returned by list APIs."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.path = self.root / "secrets.json"
        self.key_path = self.root / ".secret.key"
        self._lock = threading.RLock()

    def _fernet(self) -> Fernet:
        self.root.mkdir(parents=True, exist_ok=True)
        if self.key_path.exists():
            key = self.key_path.read_bytes().strip()
        else:
            key = Fernet.generate_key()
            self.key_path.write_bytes(key)
            try:
                os.chmod(self.key_path, 0o600)
            except OSError:
                pass
        return Fernet(key)

    def _encrypt(self, value: str) -> str:
        raw = value.encode("utf-8")
        encrypted = _dpapi_encrypt(raw) if sys.platform == "win32" else self._fernet().encrypt(raw)
        return base64.urlsafe_b64encode(encrypted).decode("ascii")

    def _decrypt(self, value: str) -> str:
        encrypted = base64.urlsafe_b64decode(value.encode("ascii"))
        raw = _dpapi_decrypt(encrypted) if sys.platform == "win32" else self._fernet().decrypt(encrypted)
        return raw.decode("utf-8")

    def _read(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return {}
        values = payload.get("values") if isinstance(payload, dict) else {}
        return {
            str(key): str(value)
            for key, value in dict(values or {}).items()
            if str(key) and isinstance(value, str)
        }

    def _write(self, values: dict[str, str]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "values": values}
        tmp = self.path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def set_many(self, namespace: str, values: dict[str, Any]) -> tuple[str, ...]:
        prefix = f"{str(namespace).strip()}:"
        with self._lock:
            stored = self._read()
            for name, value in values.items():
                key = prefix + str(name).strip()
                if not key[len(prefix):]:
                    continue
                if value is None or value == "":
                    stored.pop(key, None)
                else:
                    stored[key] = self._encrypt(str(value))
            self._write(stored)
            return tuple(sorted(
                key[len(prefix):]
                for key in stored
                if key.startswith(prefix)
            ))

    def get_many(self, namespace: str) -> dict[str, str]:
        prefix = f"{str(namespace).strip()}:"
        with self._lock:
            stored = self._read()
            result: dict[str, str] = {}
            for key, value in stored.items():
                if not key.startswith(prefix):
                    continue
                try:
                    result[key[len(prefix):]] = self._decrypt(value)
                except Exception:
                    continue
            return result

    def delete_namespace(self, namespace: str) -> None:
        prefix = f"{str(namespace).strip()}:"
        with self._lock:
            stored = {
                key: value
                for key, value in self._read().items()
                if not key.startswith(prefix)
            }
            self._write(stored)
