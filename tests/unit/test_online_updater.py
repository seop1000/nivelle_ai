from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import threading
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
ONLINE_SCRIPT = ROOT / "scripts" / "update_from_github.ps1"
APPLY_SCRIPT = ROOT / "scripts" / "apply_update.ps1"


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _installation(root: Path, version: str = "0.2.0") -> None:
    (root / "apps" / "server").mkdir(parents=True)
    (root / "apps" / "client").mkdir(parents=True)
    (root / "packages").mkdir()
    (root / "scripts").mkdir()
    shutil.copy2(APPLY_SCRIPT, root / "scripts" / "apply_update.ps1")
    (root / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        f'[project]\nversion = "{version}"\n', encoding="utf-8"
    )
    (root / "nivelle.py").write_text("launcher\n", encoding="utf-8")
    (root / "changed.py").write_text("old\n", encoding="utf-8")


def _update_zip(install: Path, from_version: str, to_version: str) -> bytes:
    payload = {
        "VERSION": f"{to_version}\n".encode(),
        "changed.py": b"new\n",
    }
    files = []
    for name, content in payload.items():
        installed = install / name
        files.append(
            {
                "path": name,
                "sha256": _sha256(content),
                "size": len(content),
                "base_sha256": _sha256(installed.read_bytes()),
            }
        )
    manifest = {
        "format_version": 1,
        "product": (
            "Nozomi"
            if from_version == "0.3.1" and to_version == "0.4.0"
            else "Nivelle"
        ),
        "from_version": from_version,
        "to_version": to_version,
        "files": files,
        "deletions": [],
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        for name, content in payload.items():
            archive.writestr(f"payload/{name}", content)
    return output.getvalue()


class _ReleaseHandler(BaseHTTPRequestHandler):
    routes: dict[str, tuple[int, str, bytes]] = {}
    authorization_headers: list[str | None] = []

    def do_GET(self) -> None:
        self.authorization_headers.append(self.headers.get("Authorization"))
        status, content_type, body = self.routes.get(
            self.path, (404, "application/json", b'{"message":"not found"}')
        )
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        del format, args


@contextmanager
def _release_server(
    package_name: str,
    package: bytes,
    checksum: bytes,
    *,
    tag: str = "v0.3.0",
    reported_package_size: int | None = None,
    package_digest: str | None = None,
) -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ReleaseHandler)
    _ReleaseHandler.authorization_headers = []
    base = f"http://127.0.0.1:{server.server_port}"
    release: dict[str, Any] = {
        "tag_name": tag,
        "draft": False,
        "prerelease": False,
        "html_url": f"{base}/release",
        "assets": [
            {
                "name": package_name,
                "size": len(package) if reported_package_size is None else reported_package_size,
                "state": "uploaded",
                "browser_download_url": f"{base}/assets/{package_name}",
            },
            {
                "name": f"{package_name}.sha256",
                "size": len(checksum),
                "state": "uploaded",
                "browser_download_url": f"{base}/assets/{package_name}.sha256",
            },
        ],
    }
    if package_digest is not None:
        release["assets"][0]["digest"] = package_digest
    _ReleaseHandler.routes = {
        "/repos/test/nivelle/releases/latest": (
            200,
            "application/json",
            json.dumps(release).encode(),
        ),
        f"/assets/{package_name}": (200, "application/zip", package),
        f"/assets/{package_name}.sha256": (200, "text/plain", checksum),
    }
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield base
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
        _ReleaseHandler.routes = {}


def _quote(value: object) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _run_online(
    state: Path,
    install: Path,
    api_base: str,
    *extra: str,
    allow_http: bool = True,
) -> subprocess.CompletedProcess[str]:
    arguments = [
        "-Repository",
        "test/nivelle",
        "-TargetRoot",
        str(install),
        "-ApiBaseUrl",
        api_base,
    ]
    if allow_http:
        arguments.append("-AllowHttpForTesting")
    arguments.extend(extra)
    rendered = [value if value.startswith("-") else _quote(value) for value in arguments]
    command = (
        f"$env:LOCALAPPDATA = {_quote(state)}; & {_quote(ONLINE_SCRIPT)} "
        + " ".join(rendered)
    )
    return subprocess.run(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )


@pytest.mark.skipif(not hasattr(subprocess, "CREATE_NEW_CONSOLE"), reason="Windows updater")
def test_downloads_matching_release_and_verifies_checksum(tmp_path: Path) -> None:
    install = tmp_path / "install"
    state = tmp_path / "state"
    _installation(install)
    package_name = "Nivelle-Update-0.2.0-to-0.3.0.zip"
    package = _update_zip(install, "0.2.0", "0.3.0")
    checksum = f"{_sha256(package)} *{package_name}\n".encode()
    with _release_server(package_name, package, checksum) as api_base:
        result = _run_online(state, install, api_base, "-DownloadOnly")

    assert result.returncode == 0, result.stdout + result.stderr
    download = state / "Nivelle" / "Updater" / "downloads" / package_name
    assert download.read_bytes() == package
    assert download.with_name(f"{package_name}.sha256").read_bytes() == checksum
    assert (install / "VERSION").read_text(encoding="utf-8").strip() == "0.2.0"
    assert not list(download.parent.glob("*.partial"))
    assert _ReleaseHandler.authorization_headers
    assert all(value is None for value in _ReleaseHandler.authorization_headers)


@pytest.mark.skipif(not hasattr(subprocess, "CREATE_NEW_CONSOLE"), reason="Windows updater")
def test_verified_package_is_handed_to_safe_apply_script(tmp_path: Path) -> None:
    install = tmp_path / "install"
    state = tmp_path / "state"
    _installation(install)
    package_name = "Nivelle-Update-0.2.0-to-0.3.0.zip"
    package = _update_zip(install, "0.2.0", "0.3.0")
    checksum = f"{_sha256(package)} *{package_name}\n".encode()
    with _release_server(package_name, package, checksum) as api_base:
        result = _run_online(state, install, api_base)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (install / "VERSION").read_text(encoding="utf-8").strip() == "0.3.0"
    assert (install / "changed.py").read_text(encoding="utf-8") == "new\n"
    backups = list((state / "Nivelle" / "Updater" / "backups").iterdir())
    assert len(backups) == 1


@pytest.mark.skipif(not hasattr(subprocess, "CREATE_NEW_CONSOLE"), reason="Windows updater")
def test_checksum_mismatch_never_leaves_package_or_partial(tmp_path: Path) -> None:
    install = tmp_path / "install"
    state = tmp_path / "state"
    _installation(install)
    package_name = "Nivelle-Update-0.2.0-to-0.3.0.zip"
    package = _update_zip(install, "0.2.0", "0.3.0")
    checksum = f"{'0' * 64} *{package_name}\n".encode()
    with _release_server(package_name, package, checksum) as api_base:
        result = _run_online(state, install, api_base, "-DownloadOnly")

    assert result.returncode == 1
    download_root = state / "Nivelle" / "Updater" / "downloads"
    assert not (download_root / package_name).exists()
    assert not list(download_root.glob("*.partial"))
    assert (install / "VERSION").read_text(encoding="utf-8").strip() == "0.2.0"


@pytest.mark.skipif(not hasattr(subprocess, "CREATE_NEW_CONSOLE"), reason="Windows updater")
def test_asset_over_size_limit_is_rejected_before_download(tmp_path: Path) -> None:
    install = tmp_path / "install"
    state = tmp_path / "state"
    _installation(install)
    package_name = "Nivelle-Update-0.2.0-to-0.3.0.zip"
    package = _update_zip(install, "0.2.0", "0.3.0")
    checksum = f"{_sha256(package)} *{package_name}\n".encode()
    with _release_server(
        package_name,
        package,
        checksum,
        reported_package_size=1048577,
    ) as api_base:
        result = _run_online(
            state,
            install,
            api_base,
            "-DownloadOnly",
            "-MaxPackageBytes",
            "1048576",
        )

    assert result.returncode == 1
    download_root = state / "Nivelle" / "Updater" / "downloads"
    assert not (download_root / package_name).exists()


@pytest.mark.skipif(not hasattr(subprocess, "CREATE_NEW_CONSOLE"), reason="Windows updater")
def test_current_release_returns_success_without_downloading_assets(tmp_path: Path) -> None:
    install = tmp_path / "install"
    state = tmp_path / "state"
    _installation(install)
    package_name = "Nivelle-Update-0.2.0-to-0.3.0.zip"
    package = _update_zip(install, "0.2.0", "0.3.0")
    checksum = f"{_sha256(package)} *{package_name}\n".encode()
    with _release_server(package_name, package, checksum, tag="v0.2.0") as api_base:
        result = _run_online(state, install, api_base)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (install / "VERSION").read_text(encoding="utf-8").strip() == "0.2.0"
    download_root = state / "Nivelle" / "Updater" / "downloads"
    assert not list(download_root.glob("*.zip"))
    assert not list(download_root.glob("*.partial"))


@pytest.mark.skipif(not hasattr(subprocess, "CREATE_NEW_CONSOLE"), reason="Windows updater")
def test_github_asset_digest_must_match_mandatory_sidecar(tmp_path: Path) -> None:
    install = tmp_path / "install"
    state = tmp_path / "state"
    _installation(install)
    package_name = "Nivelle-Update-0.2.0-to-0.3.0.zip"
    package = _update_zip(install, "0.2.0", "0.3.0")
    checksum = f"{_sha256(package)} *{package_name}\n".encode()
    with _release_server(
        package_name,
        package,
        checksum,
        package_digest=f"sha256:{'f' * 64}",
    ) as api_base:
        result = _run_online(state, install, api_base, "-DownloadOnly")

    assert result.returncode == 1
    download_root = state / "Nivelle" / "Updater" / "downloads"
    assert not (download_root / package_name).exists()
    assert not list(download_root.glob("*.partial"))


@pytest.mark.skipif(not hasattr(subprocess, "CREATE_NEW_CONSOLE"), reason="Windows updater")
def test_plain_http_is_refused_without_loopback_test_switch(tmp_path: Path) -> None:
    install = tmp_path / "install"
    state = tmp_path / "state"
    _installation(install)
    package_name = "Nivelle-Update-0.2.0-to-0.3.0.zip"
    package = _update_zip(install, "0.2.0", "0.3.0")
    checksum = f"{_sha256(package)} *{package_name}\n".encode()
    with _release_server(package_name, package, checksum) as api_base:
        result = _run_online(
            state,
            install,
            api_base,
            "-CheckOnly",
            allow_http=False,
        )

    assert result.returncode == 1
    assert not _ReleaseHandler.authorization_headers


@pytest.mark.skipif(not hasattr(subprocess, "CREATE_NEW_CONSOLE"), reason="Windows updater")
def test_031_client_discovers_exact_legacy_bridge_asset(tmp_path: Path) -> None:
    install = tmp_path / "install"
    state = tmp_path / "state"
    _installation(install, "0.3.1")
    package_name = "Nozomi-Update-0.3.1-to-0.4.0.zip"
    package = _update_zip(install, "0.3.1", "0.4.0")
    checksum = f"{_sha256(package)} *{package_name}\n".encode()
    with _release_server(package_name, package, checksum, tag="v0.4.0") as api_base:
        result = _run_online(state, install, api_base, "-CheckOnly")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "0.3.1" in result.stdout
    assert "0.4.0" in result.stdout
