import argparse

from nivelle_protocol.version import emit_startup_log

from .app import run


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Nivelle Link")
    parser.add_argument(
        "--gateway-endpoint",
        help="Gateway HTTP endpoint (overrides environment and local profiles)",
    )
    parser.add_argument(
        "--local-mode",
        action="store_true",
        help="Use the loopback Core and complete same-PC pairing automatically",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    emit_startup_log("nivelle-link")
    run(gateway_endpoint=args.gateway_endpoint, local_mode=args.local_mode)


if __name__ == "__main__":
    main()
