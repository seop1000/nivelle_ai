"""Gateway network address discovery and resolution."""

from .address_detection import (
    AddressDetectionResult,
    CandidateEvaluation,
    GatewayNetworkRuntime,
    InterfaceCandidate,
    NetworkValueSource,
    collect_windows_interface_candidates,
    detect_advertised_ipv4,
    format_gateway_endpoint,
    resolve_gateway_network,
    select_advertised_ipv4,
    validate_advertised_host,
    validate_bind_host,
)

__all__ = [
    "AddressDetectionResult",
    "CandidateEvaluation",
    "GatewayNetworkRuntime",
    "InterfaceCandidate",
    "NetworkValueSource",
    "collect_windows_interface_candidates",
    "detect_advertised_ipv4",
    "format_gateway_endpoint",
    "resolve_gateway_network",
    "select_advertised_ipv4",
    "validate_advertised_host",
    "validate_bind_host",
]
