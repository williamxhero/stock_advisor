"""Windows Credential Manager adapter; secrets never enter local JSON or logs."""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes


_CRED_TYPE_GENERIC = 1
_ERROR_NOT_FOUND = 1168


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR), ("LastWritten", ctypes.c_byte * 8),
        ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
        ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR),
    ]


def read_secret(target: str) -> str | None:
    if os.name != "nt":
        raise RuntimeError("Windows Credential Manager is required for Provider credentials")
    pointer = ctypes.POINTER(_CREDENTIALW)()
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    if not advapi.CredReadW(target, _CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
        error = ctypes.get_last_error()
        if error == _ERROR_NOT_FOUND:
            return None
        raise OSError(error, "CredReadW failed")
    try:
        raw = ctypes.string_at(pointer.contents.CredentialBlob, pointer.contents.CredentialBlobSize)
        return raw.decode("utf-16-le")
    finally:
        advapi.CredFree(pointer)


def write_secret(target: str, value: str) -> None:
    if not value.strip():
        raise ValueError("Provider API key must not be empty")
    if os.name != "nt":
        raise RuntimeError("Windows Credential Manager is required for Provider credentials")
    raw = value.encode("utf-16-le")
    blob = (ctypes.c_byte * len(raw)).from_buffer_copy(raw)
    credential = _CREDENTIALW()
    credential.Type = _CRED_TYPE_GENERIC
    credential.TargetName = target
    credential.CredentialBlobSize = len(raw)
    credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_byte))
    credential.Persist = 2  # enterprise persistence for the current user
    credential.UserName = "AITradingCompanion"
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    if not advapi.CredWriteW(ctypes.byref(credential), 0):
        error = ctypes.get_last_error()
        raise OSError(error, "CredWriteW failed")
