import os
import time
from typing import Any

import psutil


class TelemetryProvider:
    def __init__(self) -> None:
        self.started = time.monotonic()

    def sample(self) -> dict[str, Any]:
        process = psutil.Process(os.getpid())
        return {
            "system_ram_percent": psutil.virtual_memory().percent,
            "gateway_memory_bytes": process.memory_info().rss,
            "cpu_percent": psutil.cpu_percent(),
            "disk_percent": psutil.disk_usage(str(process.cwd())).percent,
            "gpu": None,
            "gpu_reason": "unsupported",
        }

    @property
    def uptime(self) -> float:
        return time.monotonic() - self.started
