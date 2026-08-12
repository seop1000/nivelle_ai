"""Legacy 0.3.x launcher bridge; use :mod:`nivelle` for new launches."""

from typing import Any

import nivelle as _implementation

main = _implementation.main


def __getattr__(name: str) -> Any:
    return getattr(_implementation, name)


if __name__ == "__main__":
    raise SystemExit(main())
