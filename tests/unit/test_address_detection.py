from __future__ import annotations

import json
from itertools import permutations

import pytest
from nivelle_protocol.configuration import ConfigurationError
from nivelle_protocol.network.address_detection import (
    AddressDetectionResult,
    InterfaceCandidate,
    NetworkValueSource,
    detect_advertised_ipv4,
    parse_windows_candidates,
    resolve_gateway_network,
    select_advertised_ipv4,
)


def ethernet(
    index: int,
    address: str,
    *,
    status: str | int = "Up",
    connection_state: str | int = "Connected",
    address_state: str | int = "Preferred",
    gateway: str | None = "192.168.10.1",
    route_metric: int | None = 10,
    interface_metric: int | None = 5,
) -> InterfaceCandidate:
    return InterfaceCandidate(
        interface_index=index,
        name="이더넷",
        description="Realtek PCIe GbE Family Controller",
        ipv4=address,
        prefix_length=24,
        status=status,
        connection_state=connection_state,
        address_state=address_state,
        hardware_interface=True,
        interface_type=6,
        gateway=gateway,
        has_default_route=gateway is not None,
        route_metric=route_metric,
        interface_metric=interface_metric,
    )


def wifi(
    index: int,
    address: str,
    *,
    gateway: str | None = "192.168.20.1",
    route_metric: int | None = 10,
    interface_metric: int | None = 10,
) -> InterfaceCandidate:
    return InterfaceCandidate(
        interface_index=index,
        name="Wi-Fi",
        description="Realtek 802.11ac USB NIC",
        ipv4=address,
        prefix_length=24,
        status="Up",
        connection_state="Connected",
        address_state="Preferred",
        hardware_interface=True,
        interface_type=71,
        gateway=gateway,
        has_default_route=gateway is not None,
        route_metric=route_metric,
        interface_metric=interface_metric,
    )


def vpn(index: int, address: str) -> InterfaceCandidate:
    return InterfaceCandidate(
        interface_index=index,
        name="Tailscale VPN",
        description="Tailscale Tunnel",
        ipv4=address,
        prefix_length=32,
        status="Up",
        connection_state="Connected",
        address_state="Preferred",
        interface_type=53,
        gateway=None,
        has_default_route=False,
        route_metric=1,
        interface_metric=1,
    )


def test_a_active_physical_ethernet_is_selected() -> None:
    result = select_advertised_ipv4([ethernet(9, "192.168.10.20")])

    assert result.address == "192.168.10.20"
    assert result.error is None
    assert result.evaluations[0].kind.value == "ethernet"
    assert result.evaluations[0].reason == "selected_ethernet_default_route"


def test_b_ethernet_beats_wifi_independent_of_metric_and_input_order() -> None:
    wired = ethernet(9, "192.168.10.20", route_metric=50, interface_metric=50)
    wireless = wifi(17, "192.168.20.30", route_metric=1, interface_metric=1)

    for candidates in ([wired, wireless], [wireless, wired]):
        result = select_advertised_ipv4(candidates)
        assert result.address == "192.168.10.20"


def test_c_disconnected_ethernet_falls_back_to_wifi() -> None:
    disconnected = ethernet(
        9,
        "192.168.10.20",
        status="Disconnected",
        connection_state="Disconnected",
    )

    result = select_advertised_ipv4([disconnected, wifi(17, "192.168.20.30")])

    assert result.address == "192.168.20.30"
    assert result.evaluations[0].reason == "adapter_not_up"


def test_d_vpn_is_excluded_by_default_and_is_an_explicit_last_resort() -> None:
    tunnel = vpn(23, "100.64.0.4")

    excluded = select_advertised_ipv4([tunnel])
    allowed = select_advertised_ipv4([tunnel], allow_vpn=True)
    lan_first = select_advertised_ipv4(
        [tunnel, wifi(17, "192.168.20.30")], allow_vpn=True
    )

    assert excluded.address is None
    assert excluded.evaluations[0].reason == "vpn_not_allowed"
    assert allowed.address == "100.64.0.4"
    assert lan_first.address == "192.168.20.30"


def test_e_virtual_wsl_adapter_is_never_published() -> None:
    virtual = InterfaceCandidate(
        interface_index=42,
        name="vEthernet (WSL)",
        description="Hyper-V Virtual Ethernet Adapter",
        ipv4="172.29.144.1",
        prefix_length=20,
        status="Up",
        connection_state="Connected",
        address_state="Preferred",
        hardware_interface=False,
        virtual=True,
        interface_type=6,
        gateway=None,
        has_default_route=False,
        route_metric=1,
        interface_metric=1,
    )

    result = select_advertised_ipv4([virtual, wifi(17, "192.168.20.30")])

    assert result.address == "192.168.20.30"
    assert result.evaluations[0].reason == "virtual_adapter"


def test_f_apipa_address_is_rejected_before_adapter_priority() -> None:
    apipa = ethernet(9, "169.254.12.34", gateway=None)

    result = select_advertised_ipv4([apipa, wifi(17, "192.168.20.30")])

    assert result.address == "192.168.20.30"
    assert result.evaluations[0].reason == "link_local"


def test_g_default_route_precedes_effective_metric_within_adapter_class() -> None:
    no_default = ethernet(
        9,
        "192.168.10.20",
        gateway=None,
        route_metric=1,
        interface_metric=1,
    )
    with_default = ethernet(
        11,
        "192.168.30.40",
        gateway="192.168.30.1",
        route_metric=100,
        interface_metric=100,
    )

    result = select_advertised_ipv4([no_default, with_default])

    assert result.address == "192.168.30.40"


def test_effective_metric_breaks_ties_after_default_route() -> None:
    high = ethernet(
        9,
        "192.168.10.20",
        route_metric=20,
        interface_metric=10,
    )
    low = ethernet(
        11,
        "192.168.30.40",
        gateway="192.168.30.1",
        route_metric=3,
        interface_metric=4,
    )

    assert select_advertised_ipv4([high, low]).address == "192.168.30.40"


def test_selection_is_stable_for_every_candidate_input_order() -> None:
    candidates = (
        ethernet(12, "192.168.30.40", route_metric=20, interface_metric=5),
        ethernet(9, "192.168.10.20", route_metric=10, interface_metric=5),
        wifi(17, "192.168.20.30", route_metric=1, interface_metric=1),
    )

    selected = {
        select_advertised_ipv4(list(candidate_order)).address
        for candidate_order in permutations(candidates)
    }

    assert selected == {"192.168.10.20"}


@pytest.mark.parametrize(
    (
        "cli",
        "environment",
        "local",
        "expected_host",
        "expected_source",
        "detector_calls",
    ),
    [
        (
            "192.168.40.10",
            {"NIVELLE_GATEWAY_ADVERTISED_HOST": "192.168.40.11"},
            "192.168.40.12",
            "192.168.40.10",
            NetworkValueSource.CLI,
            0,
        ),
        (
            None,
            {"NIVELLE_GATEWAY_ADVERTISED_HOST": "192.168.40.11"},
            "192.168.40.12",
            "192.168.40.11",
            NetworkValueSource.ENVIRONMENT,
            0,
        ),
        (
            None,
            {},
            "192.168.40.12",
            "192.168.40.12",
            NetworkValueSource.LOCAL_CONFIG,
            0,
        ),
        (
            None,
            {},
            None,
            "192.168.10.20",
            NetworkValueSource.AUTO_DETECTION,
            1,
        ),
    ],
)
def test_h_advertised_host_priority_is_cli_env_local_then_detection(
    cli: str | None,
    environment: dict[str, str],
    local: str | None,
    expected_host: str,
    expected_source: NetworkValueSource,
    detector_calls: int,
) -> None:
    calls = 0

    def detector() -> AddressDetectionResult:
        nonlocal calls
        calls += 1
        return select_advertised_ipv4([ethernet(9, "192.168.10.20")])

    runtime = resolve_gateway_network(
        local_bind_host="0.0.0.0",
        port=8765,
        local_advertised_host=local,
        cli_advertised_host=cli,
        environment=environment,
        detector=detector,
    )

    assert runtime.advertised_host == expected_host
    assert runtime.advertised_source is expected_source
    assert calls == detector_calls


@pytest.mark.parametrize(
    ("cli", "environment", "local", "expected"),
    [
        ("0.0.0.0", {"NIVELLE_GATEWAY_BIND": "127.0.0.2"}, "127.0.0.3", "0.0.0.0"),
        (None, {"NIVELLE_GATEWAY_BIND": "127.0.0.2"}, "127.0.0.3", "127.0.0.2"),
        (None, {}, "127.0.0.3", "127.0.0.3"),
    ],
)
def test_bind_host_has_the_same_cli_env_local_priority(
    cli: str | None,
    environment: dict[str, str],
    local: str,
    expected: str,
) -> None:
    runtime = resolve_gateway_network(
        local_bind_host=local,
        port=8765,
        local_advertised_host="192.168.10.20",
        cli_bind_host=cli,
        environment=environment,
    )

    assert runtime.bind_host == expected


def test_i_wildcard_bind_and_connectable_advertised_address_are_separate() -> None:
    runtime = resolve_gateway_network(
        local_bind_host="0.0.0.0",
        port=8765,
        local_advertised_host=None,
        environment={},
        detector=lambda: select_advertised_ipv4([ethernet(9, "192.168.10.20")]),
    )

    assert runtime.bind_endpoint == "http://0.0.0.0:8765"
    assert runtime.health_host == "127.0.0.1"
    assert runtime.advertised_endpoint == "http://192.168.10.20:8765"


def test_j_no_usable_interface_reports_unavailable_without_hostname_guess() -> None:
    detection = select_advertised_ipv4(
        [
            ethernet(9, "169.254.10.20", gateway=None),
            InterfaceCandidate(
                interface_index=1,
                name="Loopback",
                ipv4="127.0.0.1",
                status="Up",
                connection_state="Connected",
                address_state="Preferred",
                hardware_interface=False,
                interface_type=24,
            ),
        ]
    )
    runtime = resolve_gateway_network(
        local_bind_host="0.0.0.0",
        port=8765,
        local_advertised_host=None,
        environment={},
        detector=lambda: detection,
    )

    assert detection.error == "no_usable_ipv4"
    assert runtime.advertised_host is None
    assert runtime.advertised_endpoint is None
    assert runtime.advertised_source is NetworkValueSource.UNAVAILABLE


@pytest.mark.parametrize("value", ["0.0.0.0", "::", "169.254.10.20", "224.0.0.1"])
def test_explicit_non_connectable_advertised_host_is_a_configuration_error(
    value: str,
) -> None:
    with pytest.raises(ConfigurationError):
        resolve_gateway_network(
            local_bind_host="0.0.0.0",
            port=8765,
            local_advertised_host=value,
            environment={},
        )


def powershell_row(index: int, address: str) -> dict[str, object]:
    return {
        "interface_index": index,
        "name": "이더넷",
        "description": "Realtek PCIe GbE Family Controller",
        "ipv4": address,
        "prefix_length": 24,
        "status": 1,
        "connection_state": 1,
        "address_state": 4,
        "skip_as_source": False,
        "hardware_interface": True,
        "virtual": False,
        "interface_type": 6,
        "physical_medium": 14,
        "gateway": "192.168.10.1",
        "has_default_route": True,
        "route_metric": 1,
        "interface_metric": 1,
    }


def test_windows_json_zero_rows_is_an_empty_candidate_list() -> None:
    assert parse_windows_candidates("[]") == []


def test_windows_json_one_object_accepts_numeric_enum_values() -> None:
    candidates = parse_windows_candidates(json.dumps(powershell_row(9, "192.168.10.20")))

    assert len(candidates) == 1
    assert select_advertised_ipv4(candidates).address == "192.168.10.20"


def test_windows_json_many_rows_preserves_all_candidates() -> None:
    payload = json.dumps(
        [
            powershell_row(9, "192.168.10.20"),
            powershell_row(11, "192.168.30.40"),
        ]
    )

    assert [item.ipv4 for item in parse_windows_candidates(payload)] == [
        "192.168.10.20",
        "192.168.30.40",
    ]


@pytest.mark.parametrize("payload", ["{", "null", '["not-an-object"]', "{}"])
def test_malformed_windows_json_is_rejected(payload: str) -> None:
    with pytest.raises((KeyError, TypeError, ValueError, json.JSONDecodeError)):
        parse_windows_candidates(payload)


def test_collector_failure_becomes_a_safe_diagnostic_result() -> None:
    def failed_collector() -> list[InterfaceCandidate]:
        raise RuntimeError("PowerShell unavailable")

    result = detect_advertised_ipv4(collector=failed_collector)

    assert result.address is None
    assert result.error == "collector_failed: PowerShell unavailable"
