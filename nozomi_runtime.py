"""Legacy 0.3.x runtime import bridge; use :mod:`nivelle_runtime`."""

from typing import Any

import nivelle_runtime as _implementation


def __getattr__(name: str) -> Any:
    return getattr(_implementation, name)
