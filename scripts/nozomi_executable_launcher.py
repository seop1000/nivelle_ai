"""Compatibility entry point for Nozomi 0.3.1 launcher builds.

New builds freeze :mod:`nivelle_executable_launcher`. This module remains for
one transition release so an older build command or import still reaches the
same Nivelle implementation.
"""

try:
    from nivelle_executable_launcher import *  # noqa: F403
    from nivelle_executable_launcher import main
except ModuleNotFoundError:  # imported as scripts.nozomi_executable_launcher
    from scripts.nivelle_executable_launcher import *  # noqa: F403
    from scripts.nivelle_executable_launcher import main


if __name__ == "__main__":
    raise SystemExit(main())
