from __future__ import annotations

import platform
import time
from pathlib import Path
from typing import Any

import psutil


class SystemStatusProvider:
    def snapshot(
        self,
        *,
        client_display_name: str,
        app_version: str,
        started_monotonic: float,
    ) -> dict[str, Any]:
        memory = psutil.virtual_memory()
        volumes: list[dict[str, Any]] = []
        seen: set[str] = set()
        for partition in psutil.disk_partitions(all=False):
            mountpoint = partition.mountpoint
            if mountpoint in seen:
                continue
            if "remote" in partition.opts.casefold():
                continue
            seen.add(mountpoint)
            try:
                usage = psutil.disk_usage(mountpoint)
            except OSError:
                continue
            anchor = Path(mountpoint).anchor or mountpoint
            volumes.append(
                {"volume": anchor, "free_bytes": usage.free, "total_bytes": usage.total}
            )
            if len(volumes) >= 64:
                break

        battery_info: dict[str, Any] | None
        try:
            battery = psutil.sensors_battery()
        except (AttributeError, OSError):
            battery = None
        if battery is None:
            battery_info = None
        else:
            battery_info = {
                "percent": battery.percent,
                "plugged_in": battery.power_plugged,
                "seconds_left": None if battery.secsleft < 0 else battery.secsleft,
            }

        try:
            network_available: bool | None = any(
                item.isup for item in psutil.net_if_stats().values()
            )
        except (AttributeError, OSError):
            network_available = None

        return {
            "operating_system": {
                "name": platform.system(),
                "release": platform.release(),
                "architecture": platform.machine(),
            },
            "client_display_name": client_display_name,
            "cpu_percent": psutil.cpu_percent(interval=None),
            "ram": {
                "used_bytes": memory.used,
                "total_bytes": memory.total,
                "available_bytes": memory.available,
                "percent": memory.percent,
            },
            "local_volumes": volumes,
            "battery": battery_info,
            "network": {"available": network_available},
            "link_uptime_seconds": max(0.0, time.monotonic() - started_monotonic),
            "link_version": app_version,
        }
