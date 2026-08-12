from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import psutil

from .path_security import sanitize_display_text
from .result_utils import untrusted_result


class ActiveWindowProvider(Protocol):
    def get_metadata(self) -> dict[str, Any] | None: ...


class WindowsActiveWindowProvider:
    """One-shot foreground metadata. It never captures contents or input."""

    def get_metadata(self) -> dict[str, Any] | None:
        if os.name != "nt":
            return None
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        window = user32.GetForegroundWindow()
        if not window:
            return None
        length = user32.GetWindowTextLengthW(window)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(window, buffer, len(buffer))
        process_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId(window, ctypes.byref(process_id))
        try:
            process = psutil.Process(process_id.value)
            process_name = process.name()
            executable_basename = Path(process.exe()).name
        except (psutil.Error, OSError):
            process_name = None
            executable_basename = None
        return {
            "title": sanitize_display_text(buffer.value),
            "process_name": sanitize_display_text(process_name) if process_name else None,
            "process_id": process_id.value,
            "executable_basename": (
                sanitize_display_text(executable_basename) if executable_basename else None
            ),
            "timestamp": datetime.now(UTC).isoformat(),
        }


def get_active_window(provider: ActiveWindowProvider) -> dict[str, Any]:
    metadata = provider.get_metadata()
    if metadata is None:
        return untrusted_result(
            "get_active_window",
            content={
                "window_found": False,
                "title": None,
                "process_name": None,
                "process_id": None,
                "executable_basename": None,
                "timestamp": datetime.now(UTC).isoformat(),
            },
        )
    allowed = {
        "window_found": True,
        "title": sanitize_display_text(str(metadata.get("title", "")))[:1_000],
        "process_name": (
            sanitize_display_text(str(metadata["process_name"]))[:260]
            if metadata.get("process_name") is not None
            else None
        ),
        "process_id": int(metadata["process_id"]) if metadata.get("process_id") else None,
        "executable_basename": (
            sanitize_display_text(Path(str(metadata["executable_basename"])).name)[:260]
            if metadata.get("executable_basename") is not None
            else None
        ),
        "timestamp": str(metadata.get("timestamp") or datetime.now(UTC).isoformat()),
    }
    return untrusted_result(
        "get_active_window",
        content=allowed,
        returned_size=len(str(allowed)),
    )
