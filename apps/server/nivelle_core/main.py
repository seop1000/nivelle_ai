import argparse
from collections.abc import Sequence

import uvicorn
from nivelle_protocol.configuration import ConfigurationError
from nivelle_protocol.network import GatewayNetworkRuntime, resolve_gateway_network
from nivelle_protocol.settings import ModelsSettings, ServerSettings
from nivelle_protocol.version import emit_startup_log

from .app import create_app
from .paths import server_data_dir


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Nivelle Core Gateway")
    parser.add_argument(
        "--provider-endpoint",
        help="Model provider HTTP endpoint (overrides environment and local config)",
    )
    parser.add_argument(
        "--gateway-bind",
        help="Gateway bind host (overrides NIVELLE_GATEWAY_BIND and server.yaml)",
    )
    parser.add_argument(
        "--gateway-advertised-host",
        help="Concrete Link host; blank uses Windows adapter auto-detection",
    )
    parser.add_argument(
        "--network-diagnostics",
        action="store_true",
        help="Print Gateway bind/address selection details and exit",
    )
    parser.add_argument(
        "--ui",
        action="store_true",
        help="Open the local Core identity and authentication administration UI",
    )
    return parser.parse_args(argv)


def _print_network_diagnostics(
    network: GatewayNetworkRuntime, *, provider_endpoint: str
) -> None:
    print("Nivelle Core network diagnostics")
    print(f"bind={network.bind_endpoint}")
    print(
        "advertised="
        f"{network.advertised_endpoint or 'unavailable'} "
        f"source={network.advertised_source.value}"
    )
    print(f"provider={provider_endpoint}")
    if network.detection.error:
        print(f"detection_error={network.detection.error}")
    for evaluation in network.detection.evaluations:
        candidate = evaluation.candidate
        print(
            "candidate="
            f"{candidate.name!r} ifIndex={candidate.interface_index} "
            f"ipv4={candidate.ipv4} kind={evaluation.kind.value} "
            f"gateway={candidate.gateway or '-'} metric={candidate.effective_metric} "
            f"eligible={str(evaluation.eligible).lower()} reason={evaluation.reason}"
        )


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    root = server_data_dir()
    app = create_app(root, provider_endpoint_override=args.provider_endpoint)
    settings = ServerSettings.model_validate(app.state.services.config.load("server"))
    models = ModelsSettings.model_validate(app.state.services.config.load("models"))
    try:
        network = resolve_gateway_network(
            local_bind_host=settings.host,
            port=settings.port,
            local_advertised_host=settings.advertised_host,
            cli_bind_host=args.gateway_bind,
            cli_advertised_host=args.gateway_advertised_host,
        )
    except ConfigurationError as exc:
        print(f"Nivelle Core network configuration error: {exc}")
        return 2
    app.state.services.network_runtime = network
    _print_network_diagnostics(network, provider_endpoint=models.provider_endpoint)
    if args.network_diagnostics:
        return 0 if network.advertised_host is not None else 2
    emit_startup_log("nivelle-core")
    print(f"Nivelle Core data: {root}")
    resolved = app.state.services.config.resolved_sources.get("provider_endpoint")
    if resolved is not None:
        print(resolved.diagnostic())
    if args.ui:
        from .admin_ui import run_core_admin_ui

        return run_core_admin_ui(
            app,
            host=network.bind_host,
            port=network.port,
            log_level=settings.log_level.lower(),
        )
    uvicorn.run(
        app,
        host=network.bind_host,
        port=network.port,
        log_level=settings.log_level.lower(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
