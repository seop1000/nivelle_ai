"""Legacy 0.3.x import bridge for :mod:`nivelle_link`."""

from nivelle_link import APP_VERSION
from nivelle_link import __path__ as _canonical_path

__path__ = list(_canonical_path)
__all__ = ["APP_VERSION"]
