"""Legacy 0.3.x import bridge for the renamed Nivelle protocol package.

New code must import :mod:`nivelle_protocol`. This module remains for one
transition release so installed extensions do not fail before they update.
"""

import sys
from importlib import import_module

from nivelle_protocol import *  # noqa: F403
from nivelle_protocol import __all__ as __all__

_SUBMODULES = (
    "chat",
    "envelopes",
    "errors",
    "identity",
    "local_migration",
    "memory",
    "pairing",
    "persona",
    "server_status",
    "settings",
    "tools",
    "version",
)
for _submodule in _SUBMODULES:
    sys.modules[f"{__name__}.{_submodule}"] = import_module(
        f"nivelle_protocol.{_submodule}"
    )

del _submodule
