import io
import json
import tomllib
from pathlib import Path

import pytest
from nivelle_protocol.version import (
    APP_VERSION,
    emit_startup_log,
    is_protocol_compatible,
    protocol_compatibility,
    protocol_major,
    runtime_identity,
)

ROOT = Path(__file__).resolve().parents[2]


def test_release_version_sources_are_consistent() -> None:
    version_file = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert version_file == APP_VERSION
    assert "version" in project["project"]["dynamic"]
    assert project["tool"]["hatch"]["version"]["path"] == "VERSION"
    assert "version" not in project["project"]


def test_protocol_compatibility_uses_major_version_boundary() -> None:
    assert protocol_major("1.7") == 1
    assert is_protocol_compatible("1.9") is True
    warning = protocol_compatibility("1.9")
    assert warning.status == "version_warning"
    assert warning.warning is not None
    mismatch = protocol_compatibility("2.0")
    assert mismatch.compatible is False
    assert mismatch.status == "major_mismatch"
    invalid = protocol_compatibility("unknown")
    assert invalid.compatible is False
    assert invalid.status == "invalid"
    with pytest.raises(ValueError):
        protocol_major("1.x")


def test_runtime_identity_uses_available_build_metadata_and_executable(tmp_path: Path) -> None:
    executable = tmp_path / "Nivelle-Core.exe"
    identity = runtime_identity(
        "nivelle-core",
        environ={
            "NIVELLE_BUILD_COMMIT": "abc1234",
            "NIVELLE_BUILD_TIME": "2026-08-03T10:00:00Z",
        },
        executable=executable,
        frozen=True,
    )

    assert identity.component == "nivelle-core"
    assert identity.app_version == "0.4.0"
    assert identity.build_commit == "abc1234"
    assert identity.build_time == "2026-08-03T10:00:00Z"
    assert identity.executable_path == str(executable.resolve())
    assert identity.frozen is True


def test_startup_log_is_structured_and_contains_no_unrelated_environment_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NIVELLE_BUILD_COMMIT", "def5678")
    monkeypatch.setenv("NIVELLE_AUTH_TOKEN", "must-not-be-logged")
    output = io.StringIO()

    line = emit_startup_log("nivelle-link", stream=output)
    record = json.loads(line)

    assert output.getvalue() == line + "\n"
    assert record["component"] == "nivelle-link"
    assert record["build_commit"] == "def5678"
    assert "must-not-be-logged" not in line
