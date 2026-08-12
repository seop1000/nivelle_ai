"""Deterministic Windows IPv4 discovery for the Nivelle Gateway.

The Gateway's bind address and its advertised address are deliberately separate:
``0.0.0.0`` is useful for listening but is never a connectable endpoint.  This
module does not use host-name resolution because a Windows host can resolve to
multiple adapters in an unstable order.
"""

from __future__ import annotations

import ipaddress
import json
import os
import platform
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from nivelle_protocol.configuration import ConfigurationError, SettingSource


class InterfaceKind(StrEnum):
    ETHERNET = "ethernet"
    WIFI = "wifi"
    VPN = "vpn"
    OTHER = "other"


class NetworkValueSource(StrEnum):
    CLI = SettingSource.CLI.value
    ENVIRONMENT = SettingSource.ENVIRONMENT.value
    LOCAL_CONFIG = SettingSource.LOCAL_CONFIG.value
    AUTO_DETECTION = "auto_detection"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class InterfaceCandidate:
    """One IPv4 address attached to one Windows interface."""

    interface_index: int
    name: str
    ipv4: str
    description: str = ""
    prefix_length: int | None = None
    status: str | int = "up"
    connection_state: str | int = "connected"
    address_state: str | int = "preferred"
    skip_as_source: bool = False
    hardware_interface: bool = False
    virtual: bool = False
    interface_type: int | None = None
    physical_medium: int | None = None
    gateway: str | None = None
    has_default_route: bool = False
    route_metric: int | None = None
    interface_metric: int | None = None

    @property
    def effective_metric(self) -> int:
        return (self.route_metric or 0) + (self.interface_metric or 0)


@dataclass(frozen=True, slots=True)
class CandidateEvaluation:
    candidate: InterfaceCandidate
    eligible: bool
    kind: InterfaceKind
    reason: str
    rank: tuple[int, int, int, int, int] | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.candidate.name,
            "interface_index": self.candidate.interface_index,
            "ipv4": self.candidate.ipv4,
            "kind": self.kind.value,
            "eligible": self.eligible,
            "reason": self.reason,
            "gateway": self.candidate.gateway,
            "has_default_route": self.candidate.has_default_route,
            "route_metric": self.candidate.route_metric,
            "interface_metric": self.candidate.interface_metric,
        }


@dataclass(frozen=True, slots=True)
class AddressDetectionResult:
    selected: InterfaceCandidate | None
    evaluations: tuple[CandidateEvaluation, ...] = ()
    error: str | None = None

    @property
    def address(self) -> str | None:
        return self.selected.ipv4 if self.selected is not None else None


@dataclass(frozen=True, slots=True)
class GatewayNetworkRuntime:
    bind_host: str
    port: int
    advertised_host: str | None
    advertised_source: NetworkValueSource
    detection: AddressDetectionResult

    @property
    def bind_endpoint(self) -> str:
        return format_gateway_endpoint(self.bind_host, self.port)

    @property
    def advertised_endpoint(self) -> str | None:
        if self.advertised_host is None:
            return None
        return format_gateway_endpoint(self.advertised_host, self.port)

    @property
    def health_host(self) -> str:
        if self.bind_host in {"0.0.0.0", "::", "[::]"}:
            return "127.0.0.1"
        return self.bind_host

    def status_dict(self) -> dict[str, object]:
        selected = self.detection.selected
        return {
            "bind_host": self.bind_host,
            "bind_port": self.port,
            "bind_endpoint": self.bind_endpoint,
            "advertised_host": self.advertised_host,
            "advertised_endpoint": self.advertised_endpoint,
            "advertised_source": self.advertised_source.value,
            "selected_interface": (
                {
                    "name": selected.name,
                    "interface_index": selected.interface_index,
                    "kind": _classify_interface(selected).value,
                    "ipv4": selected.ipv4,
                    "gateway": selected.gateway,
                    "effective_metric": selected.effective_metric,
                }
                if selected is not None
                else None
            ),
            "detection_error": self.detection.error,
            "candidates": [item.as_dict() for item in self.detection.evaluations],
        }


Collector = Callable[[], Sequence[InterfaceCandidate]]

_VPN_PATTERN = re.compile(
    r"\b(vpn|tailscale|zerotier|wireguard|openvpn|tap|tun|ppp)\b", re.IGNORECASE
)
_VIRTUAL_PATTERN = re.compile(
    r"(virtual|vEthernet|Hyper-V|VMware|VirtualBox|WSL|Wi-Fi Direct|Bluetooth)",
    re.IGNORECASE,
)


def _state_matches(value: str | int, expected: str, numeric: set[int]) -> bool:
    if isinstance(value, int):
        return value in numeric
    normalized = str(value).strip().lower()
    return normalized == expected or normalized in {str(item) for item in numeric}


def _classify_interface(candidate: InterfaceCandidate) -> InterfaceKind:
    label = f"{candidate.name} {candidate.description}"
    if _VPN_PATTERN.search(label):
        return InterfaceKind.VPN
    is_virtual = candidate.virtual or bool(_VIRTUAL_PATTERN.search(label))
    # Windows IF_TYPE_ETHERNET_CSMACD=6 and IF_TYPE_IEEE80211=71.  The numeric
    # metadata remains stable even when the adapter alias is localized.
    if (
        candidate.interface_type == 6
        and candidate.hardware_interface
        and not is_virtual
    ):
        return InterfaceKind.ETHERNET
    if (
        candidate.interface_type == 71
        and candidate.hardware_interface
        and not is_virtual
    ):
        return InterfaceKind.WIFI
    return InterfaceKind.OTHER


def _address_rejection(address: str) -> str | None:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return "invalid_ipv4"
    if not isinstance(parsed, ipaddress.IPv4Address):
        return "not_ipv4"
    if parsed.is_unspecified:
        return "unspecified"
    if parsed.is_loopback:
        return "loopback"
    if parsed.is_link_local:
        return "link_local"
    if parsed.is_multicast:
        return "multicast"
    if parsed.is_reserved or parsed == ipaddress.IPv4Address("255.255.255.255"):
        return "reserved_or_broadcast"
    return None


def validate_bind_host(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("bind address must be a non-empty host")
    host = value.strip()
    if "://" in host or "/" in host:
        raise ValueError("bind address must be a host, not a URL")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if any(character.isspace() for character in host):
        raise ValueError("bind address must not contain whitespace")
    return host


def validate_advertised_host(
    value: object,
    *,
    allow_loopback: bool = True,
    allow_hostname: bool = True,
) -> str:
    host = validate_bind_host(value)
    if host in {"0.0.0.0", "::"}:
        raise ValueError("wildcard bind addresses cannot be advertised")
    try:
        parsed = ipaddress.ip_address(host)
    except ValueError:
        if not allow_hostname:
            raise ValueError("advertised address must be an IPv4 address") from None
        if not re.fullmatch(r"(?=.{1,253}\.?$)[A-Za-z0-9._-]+", host):
            raise ValueError("advertised host name is invalid") from None
        return host
    if isinstance(parsed, ipaddress.IPv6Address):
        if parsed.is_unspecified or parsed.is_multicast or parsed.is_link_local:
            raise ValueError("advertised IPv6 address is not connectable")
        if parsed.is_loopback and not allow_loopback:
            raise ValueError("loopback cannot be advertised to a remote Link")
        return str(parsed)
    rejection = _address_rejection(str(parsed))
    if rejection == "loopback" and allow_loopback:
        return str(parsed)
    if rejection is not None:
        raise ValueError(f"advertised IPv4 address is not connectable: {rejection}")
    return str(parsed)


def _evaluate_candidate(
    candidate: InterfaceCandidate, *, allow_vpn: bool
) -> CandidateEvaluation:
    kind = _classify_interface(candidate)
    rejection = _address_rejection(candidate.ipv4)
    if rejection is None and candidate.prefix_length not in {None, 31, 32}:
        network = ipaddress.IPv4Network(
            f"{candidate.ipv4}/{candidate.prefix_length}", strict=False
        )
        if ipaddress.IPv4Address(candidate.ipv4) == network.broadcast_address:
            rejection = "subnet_broadcast"
    if rejection is not None:
        return CandidateEvaluation(candidate, False, kind, rejection)
    if not _state_matches(candidate.status, "up", {1}):
        return CandidateEvaluation(candidate, False, kind, "adapter_not_up")
    if not _state_matches(candidate.connection_state, "connected", {1}):
        return CandidateEvaluation(candidate, False, kind, "adapter_not_connected")
    # NetIPAddress.AddressState Preferred serializes as either its name or 4.
    if not _state_matches(candidate.address_state, "preferred", {4}):
        return CandidateEvaluation(candidate, False, kind, "address_not_preferred")
    if candidate.skip_as_source:
        return CandidateEvaluation(candidate, False, kind, "skip_as_source")
    if candidate.virtual or (
        kind is InterfaceKind.OTHER
        and _VIRTUAL_PATTERN.search(f"{candidate.name} {candidate.description}")
    ):
        return CandidateEvaluation(candidate, False, kind, "virtual_adapter")
    if kind is InterfaceKind.OTHER:
        return CandidateEvaluation(candidate, False, kind, "not_physical_lan")
    if kind is InterfaceKind.VPN and not allow_vpn:
        return CandidateEvaluation(candidate, False, kind, "vpn_not_allowed")
    kind_rank = {
        InterfaceKind.ETHERNET: 0,
        InterfaceKind.WIFI: 1,
        InterfaceKind.VPN: 2,
        InterfaceKind.OTHER: 3,
    }[kind]
    address_number = int(ipaddress.IPv4Address(candidate.ipv4))
    rank = (
        kind_rank,
        0 if candidate.has_default_route and candidate.gateway else 1,
        candidate.effective_metric,
        candidate.interface_index,
        address_number,
    )
    return CandidateEvaluation(candidate, True, kind, "eligible", rank)


def select_advertised_ipv4(
    candidates: Sequence[InterfaceCandidate], *, allow_vpn: bool = False
) -> AddressDetectionResult:
    evaluations = [_evaluate_candidate(candidate, allow_vpn=allow_vpn) for candidate in candidates]
    eligible = [item for item in evaluations if item.eligible and item.rank is not None]
    if not eligible:
        return AddressDetectionResult(
            selected=None,
            evaluations=tuple(evaluations),
            error="no_usable_ipv4",
        )
    selected_evaluation = min(eligible, key=lambda item: item.rank or (99, 99, 99, 99, 99))
    finalized = tuple(
        CandidateEvaluation(
            item.candidate,
            True,
            item.kind,
            "selected_"
            f"{item.kind.value}"
            f"{'_default_route' if item.candidate.has_default_route else ''}",
            item.rank,
        )
        if item is selected_evaluation
        else item
        if not item.eligible
        else CandidateEvaluation(item.candidate, True, item.kind, "lower_priority", item.rank)
        for item in evaluations
    )
    return AddressDetectionResult(selected_evaluation.candidate, finalized)


_WINDOWS_COLLECTOR = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$adapters = @(Get-NetAdapter -IncludeHidden)
$addresses = @(Get-NetIPAddress -AddressFamily IPv4)
$routes = @(Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue)
$interfaces = @(Get-NetIPInterface -AddressFamily IPv4)
$rows = foreach ($address in $addresses) {
  $adapter = $adapters | Where-Object ifIndex -EQ $address.InterfaceIndex | Select-Object -First 1
  if ($null -eq $adapter) { continue }
  $interface = $interfaces | Where-Object InterfaceIndex -EQ $address.InterfaceIndex | Select-Object -First 1
  $route = $routes | Where-Object {
    $_.InterfaceIndex -eq $address.InterfaceIndex -and [string]$_.State -eq 'Alive'
  } | Sort-Object RouteMetric | Select-Object -First 1
  [PSCustomObject]@{
    interface_index = [int]$address.InterfaceIndex
    name = [string]$adapter.Name
    description = [string]$adapter.InterfaceDescription
    ipv4 = [string]$address.IPAddress
    prefix_length = [int]$address.PrefixLength
    status = [string]$adapter.Status
    connection_state = [string]$adapter.MediaConnectionState
    address_state = [string]$address.AddressState
    skip_as_source = [bool]$address.SkipAsSource
    hardware_interface = [bool]$adapter.HardwareInterface
    virtual = [bool]$adapter.Virtual
    interface_type = [int]$adapter.InterfaceType
    physical_medium = [int]$adapter.NdisPhysicalMedium
    gateway = if ($null -ne $route) { [string]$route.NextHop } else { $null }
    has_default_route = ($null -ne $route)
    route_metric = if ($null -ne $route) { [int]$route.RouteMetric } else { $null }
    interface_metric = if ($null -ne $interface) { [int]$interface.InterfaceMetric } else { $null }
  }
}
@($rows) | ConvertTo-Json -Compress
"""


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def parse_windows_candidates(payload: str) -> list[InterfaceCandidate]:
    if not payload.strip():
        return []
    decoded = json.loads(payload)
    rows = decoded if isinstance(decoded, list) else [decoded]
    candidates: list[InterfaceCandidate] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Windows adapter result contains a non-object row")
        candidates.append(
            InterfaceCandidate(
                interface_index=int(row["interface_index"]),
                name=str(row.get("name") or ""),
                description=str(row.get("description") or ""),
                ipv4=str(row["ipv4"]),
                prefix_length=_optional_int(row.get("prefix_length")),
                status=row.get("status", ""),
                connection_state=row.get("connection_state", ""),
                address_state=row.get("address_state", ""),
                skip_as_source=bool(row.get("skip_as_source", False)),
                hardware_interface=bool(row.get("hardware_interface", False)),
                virtual=bool(row.get("virtual", False)),
                interface_type=_optional_int(row.get("interface_type")),
                physical_medium=_optional_int(row.get("physical_medium")),
                gateway=str(row["gateway"]) if row.get("gateway") else None,
                has_default_route=bool(row.get("has_default_route", False)),
                route_metric=_optional_int(row.get("route_metric")),
                interface_metric=_optional_int(row.get("interface_metric")),
            )
        )
    return candidates


def collect_windows_interface_candidates() -> list[InterfaceCandidate]:
    if platform.system() != "Windows":
        raise RuntimeError("Windows network adapter discovery is unavailable on this platform")
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _WINDOWS_COLLECTOR],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or f"exit code {completed.returncode}"
        raise RuntimeError(f"Windows network discovery failed: {detail}")
    try:
        return parse_windows_candidates(completed.stdout)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Windows network discovery returned invalid JSON: {exc}") from exc


def detect_advertised_ipv4(
    *, collector: Collector = collect_windows_interface_candidates, allow_vpn: bool = False
) -> AddressDetectionResult:
    try:
        candidates = collector()
    except Exception as exc:
        return AddressDetectionResult(None, error=f"collector_failed: {exc}")
    return select_advertised_ipv4(candidates, allow_vpn=allow_vpn)


def format_gateway_endpoint(host: str, port: int) -> str:
    rendered = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"http://{rendered}:{port}"


def resolve_gateway_network(
    *,
    local_bind_host: str,
    port: int,
    local_advertised_host: str | None,
    cli_bind_host: str | None = None,
    cli_advertised_host: str | None = None,
    environment: Mapping[str, str] | None = None,
    detector: Callable[[], AddressDetectionResult] = detect_advertised_ipv4,
) -> GatewayNetworkRuntime:
    environ = os.environ if environment is None else environment
    bind_candidates = (
        (NetworkValueSource.CLI, cli_bind_host),
        (NetworkValueSource.ENVIRONMENT, environ.get("NIVELLE_GATEWAY_BIND")),
        (NetworkValueSource.LOCAL_CONFIG, local_bind_host),
    )
    bind_host: str | None = None
    for source, value in bind_candidates:
        if value is not None and str(value).strip():
            try:
                bind_host = validate_bind_host(value)
            except ValueError as exc:
                raise ConfigurationError(
                    f"gateway bind from {source.value} is invalid: {exc}"
                ) from exc
            break
    if bind_host is None:
        bind_host = "0.0.0.0"

    configured = (
        (NetworkValueSource.CLI, cli_advertised_host),
        (
            NetworkValueSource.ENVIRONMENT,
            environ.get("NIVELLE_GATEWAY_ADVERTISED_HOST"),
        ),
        (NetworkValueSource.LOCAL_CONFIG, local_advertised_host),
    )
    for source, value in configured:
        if value is None or not str(value).strip():
            continue
        try:
            advertised = validate_advertised_host(value)
        except ValueError as exc:
            raise ConfigurationError(
                f"gateway advertised host from {source.value} is invalid: {exc}"
            ) from exc
        return GatewayNetworkRuntime(
            bind_host,
            port,
            advertised,
            source,
            AddressDetectionResult(None),
        )

    detection = detector()
    return GatewayNetworkRuntime(
        bind_host,
        port,
        detection.address,
        (
            NetworkValueSource.AUTO_DETECTION
            if detection.address is not None
            else NetworkValueSource.UNAVAILABLE
        ),
        detection,
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
    "parse_windows_candidates",
    "resolve_gateway_network",
    "select_advertised_ipv4",
    "validate_advertised_host",
    "validate_bind_host",
]
