"""Install and locate Nivelle's pinned local inference runtime.

This module intentionally uses only Python's standard library so it can run
before the rest of Nivelle's dependencies are available.
"""

from __future__ import annotations

import hashlib
import http.client
import os
import re
import shutil
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
RUNTIME_ROOT = PROJECT_ROOT / "runtime"

PRIMARY_MODEL_URL = (
    "https://huggingface.co/mistralai/Ministral-3-14B-Instruct-2512-GGUF/resolve/"
    "main/Ministral-3-14B-Instruct-2512-Q4_K_M.gguf?download=true"
)
PRIMARY_MODEL_SIZE = 8_200_000_000  # TODO: Replace with exact byte size
PRIMARY_MODEL_SHA256 = "0000000000000000000000000000000000000000000000000000000000000000"  # TODO: Replace with exact SHA-256
PRIMARY_MODEL_PATH = RUNTIME_ROOT / "models" / "Ministral-3-14B-Instruct-2512-Q4_K_M.gguf"

FALLBACK_MODEL_URL = (
    "https://huggingface.co/bartowski/Qwen_Qwen3.5-9B-GGUF/resolve/"
    "182be2fd6c7bc44887d88a91cb03ff009cc9f549/"
    "Qwen_Qwen3.5-9B-Q4_K_M.gguf?download=true"
)
FALLBACK_MODEL_SIZE = 6_169_341_984
FALLBACK_MODEL_SHA256 = "d784ce9eda1a5a7b51e8f705a9e6310844bf4f173654d115823c775fdea56d43"
FALLBACK_MODEL_PATH = RUNTIME_ROOT / "models" / "Qwen_Qwen3.5-9B-Q4_K_M.gguf"

# Compatibility names retained for tools that imported the original single-model constants.
MODEL_URL = PRIMARY_MODEL_URL
MODEL_SIZE = PRIMARY_MODEL_SIZE
MODEL_SHA256 = PRIMARY_MODEL_SHA256
MODEL_PATH = PRIMARY_MODEL_PATH

LLAMA_VERSION = "b10231"
LLAMA_URL = (
    "https://github.com/ggml-org/llama.cpp/releases/download/b10231/"
    "llama-b10231-bin-win-vulkan-x64.zip"
)
LLAMA_SIZE = 34_113_959
LLAMA_SHA256 = "f7c6e342638f800cb62a01ad607c1fbdf2cd6d4324062f5649459982a74aa370"
LLAMA_ARCHIVE_PATH = RUNTIME_ROOT / "downloads" / "llama-b10231-bin-win-vulkan-x64.zip"
LLAMA_INSTALL_PATH = RUNTIME_ROOT / "llama.cpp" / LLAMA_VERSION

_CHUNK_SIZE = 8 * 1024 * 1024
_DOWNLOAD_ATTEMPTS = 4
_HTTP_TIMEOUT_SECONDS = 60
_CONTENT_RANGE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", re.IGNORECASE)


class RuntimeInstallError(RuntimeError):
    """Raised when a required runtime artifact cannot be installed safely."""


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    """Paths to the installed model and llama.cpp server executable."""

    model_path: Path
    server_path: Path
    fallback_model_path: Path | None = None


class _DownloadProgress:
    def __init__(self, label: str, initial: int, total: int) -> None:
        self.label = label
        self.initial = initial
        self.total = total
        self.started_at = time.monotonic()
        self.last_update = 0.0
        self._line_open = False

    def update(self, completed: int, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and completed < self.total and now - self.last_update < 0.5:
            return
        elapsed = max(now - self.started_at, 0.001)
        session_bytes = max(completed - self.initial, 0)
        speed = session_bytes / elapsed
        percent = min(completed / self.total * 100, 100.0)
        message = (
            f"\r{self.label}: {percent:6.2f}% "
            f"({_format_bytes(completed)} / {_format_bytes(self.total)}, "
            f"{_format_bytes(speed)}/s)"
        )
        sys.stdout.write(message)
        sys.stdout.flush()
        self.last_update = now
        self._line_open = True

    def finish_line(self) -> None:
        if self._line_open:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self._line_open = False


def _format_bytes(value: float) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    amount = float(value)
    for unit in units[:-1]:
        if abs(amount) < 1024:
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} {units[-1]}"


def _verification_stamp_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.verified")


def _stamp_is_current(path: Path, expected_size: int, expected_sha256: str) -> bool:
    stamp_path = _verification_stamp_path(path)
    try:
        file_stat = path.stat()
        fields = stamp_path.read_text(encoding="ascii").strip().split()
    except (OSError, UnicodeError):
        return False
    return fields == [expected_sha256, str(expected_size), str(file_stat.st_mtime_ns)]


def _write_verification_stamp(path: Path, expected_size: int, expected_sha256: str) -> None:
    stamp_path = _verification_stamp_path(path)
    temporary = stamp_path.with_name(f".{stamp_path.name}.{uuid.uuid4().hex}.tmp")
    content = f"{expected_sha256} {expected_size} {path.stat().st_mtime_ns}\n"
    try:
        temporary.write_text(content, encoding="ascii")
        os.replace(temporary, stamp_path)
    finally:
        temporary.unlink(missing_ok=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_handle:
        while chunk := file_handle.read(_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _verified_file(
    path: Path,
    expected_size: int,
    expected_sha256: str,
    *,
    label: str,
    allow_stamp: bool = True,
) -> bool:
    if path.is_symlink():
        raise RuntimeInstallError(f"Refusing to use a symbolic link as {label}: {path}")
    try:
        if not path.is_file() or path.stat().st_size != expected_size:
            return False
    except OSError as exc:
        raise RuntimeInstallError(f"Could not inspect {label}: {path}") from exc

    if allow_stamp and _stamp_is_current(path, expected_size, expected_sha256):
        return True

    print(f"Verifying {label} SHA-256: {path}")
    if _sha256(path).casefold() != expected_sha256.casefold():
        return False
    if allow_stamp:
        _write_verification_stamp(path, expected_size, expected_sha256)
    return True


def _require_free_space(directory: Path, required_bytes: int, purpose: str) -> None:
    if required_bytes <= 0:
        return
    try:
        free_bytes = shutil.disk_usage(directory).free
    except OSError as exc:
        raise RuntimeInstallError(f"Could not check free space in {directory}") from exc
    if free_bytes < required_bytes:
        raise RuntimeInstallError(
            f"Not enough free space to {purpose}. "
            f"Required: {_format_bytes(required_bytes)}; available: {_format_bytes(free_bytes)}."
        )


def _validate_content_range(header: str | None, start: int, expected_size: int) -> None:
    match = _CONTENT_RANGE.fullmatch((header or "").strip())
    if match is None:
        raise RuntimeInstallError("The download server returned an invalid Content-Range header.")
    returned_start, returned_end, returned_total = match.groups()
    if int(returned_start) != start or int(returned_end) < start:
        raise RuntimeInstallError("The download server resumed from an unexpected byte offset.")
    if returned_total != "*" and int(returned_total) != expected_size:
        raise RuntimeInstallError("The download server reported an unexpected artifact size.")


def _download_once(url: str, part_path: Path, expected_size: int, label: str) -> None:
    start = part_path.stat().st_size if part_path.exists() else 0
    headers = {"User-Agent": "Nivelle/0.4 runtime-installer"}
    if start:
        headers["Range"] = f"bytes={start}-"
    request = urllib.request.Request(url, headers=headers)

    with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_SECONDS) as response:
        status = getattr(response, "status", None)
        if status is None:
            status = response.getcode()

        if status == http.client.PARTIAL_CONTENT:
            _validate_content_range(response.headers.get("Content-Range"), start, expected_size)
            mode = "ab"
        elif status == http.client.OK:
            if start:
                print(f"{label}: server ignored Range; restarting this download.")
            start = 0
            mode = "wb"
        else:
            raise RuntimeInstallError(f"Unexpected HTTP status {status} while downloading {label}.")

        progress = _DownloadProgress(label, start, expected_size)
        completed = start
        progress.update(completed, force=True)
        try:
            with part_path.open(mode) as output:
                while chunk := response.read(_CHUNK_SIZE):
                    output.write(chunk)
                    completed += len(chunk)
                    progress.update(completed)
                    if completed > expected_size:
                        raise RuntimeInstallError(
                            f"{label} exceeded its expected size; refusing the download."
                        )
                output.flush()
                os.fsync(output.fileno())
            progress.update(completed, force=True)
        finally:
            progress.finish_line()


def _prepare_download_target(target: Path, part_path: Path, expected_size: int) -> None:
    if target.exists():
        if not target.is_file() or target.is_symlink():
            raise RuntimeInstallError(f"The runtime artifact path is not a regular file: {target}")
        _verification_stamp_path(target).unlink(missing_ok=True)
        if target.stat().st_size < expected_size and not part_path.exists():
            os.replace(target, part_path)
        else:
            target.unlink()

    if part_path.exists():
        if not part_path.is_file() or part_path.is_symlink():
            raise RuntimeInstallError(
                f"The partial download path is not a regular file: {part_path}"
            )
        if part_path.stat().st_size > expected_size:
            part_path.unlink()


def _finish_download(
    target: Path,
    part_path: Path,
    expected_size: int,
    expected_sha256: str,
    label: str,
) -> bool:
    if not part_path.exists() or part_path.stat().st_size != expected_size:
        return False
    if not _verified_file(
        part_path,
        expected_size,
        expected_sha256,
        label=label,
        allow_stamp=False,
    ):
        part_path.unlink(missing_ok=True)
        return False
    os.replace(part_path, target)
    _write_verification_stamp(target, expected_size, expected_sha256)
    return True


def _ensure_download(
    url: str,
    target: Path,
    expected_size: int,
    expected_sha256: str,
    label: str,
) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    if _verified_file(target, expected_size, expected_sha256, label=label):
        print(f"{label} is already installed.")
        return target

    part_path = target.with_name(f"{target.name}.part")
    _prepare_download_target(target, part_path, expected_size)
    if _finish_download(target, part_path, expected_size, expected_sha256, label):
        return target

    last_error: BaseException | None = None
    for attempt in range(1, _DOWNLOAD_ATTEMPTS + 1):
        current_size = part_path.stat().st_size if part_path.exists() else 0
        _require_free_space(
            target.parent,
            expected_size - current_size,
            f"download {label}",
        )
        if attempt > 1:
            print(f"Retrying {label} ({attempt}/{_DOWNLOAD_ATTEMPTS})...")
        try:
            _download_once(url, part_path, expected_size, label)
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code == http.client.REQUESTED_RANGE_NOT_SATISFIABLE:
                part_path.unlink(missing_ok=True)
            elif 400 <= exc.code < 500:
                raise RuntimeInstallError(
                    f"HTTP {exc.code} while downloading {label}: {url}"
                ) from exc
        except (http.client.HTTPException, OSError, urllib.error.URLError) as exc:
            last_error = exc

        if part_path.exists() and part_path.stat().st_size > expected_size:
            part_path.unlink(missing_ok=True)
            raise RuntimeInstallError(f"Downloaded {label} was larger than expected.")
        if _finish_download(target, part_path, expected_size, expected_sha256, label):
            return target

        if last_error is None:
            current_size = part_path.stat().st_size if part_path.exists() else 0
            last_error = RuntimeInstallError(
                f"{label} download ended early at {current_size} of {expected_size} bytes."
            )
        if attempt < _DOWNLOAD_ATTEMPTS:
            time.sleep(min(float(attempt), 3.0))

    raise RuntimeInstallError(
        f"Could not download {label} after {_DOWNLOAD_ATTEMPTS} attempts. "
        "The partial file was kept so a later launch can resume it."
    ) from last_error


def _member_destination(root: Path, member: zipfile.ZipInfo) -> tuple[Path, bool]:
    raw_name = member.filename.replace("\\", "/")
    if "\x00" in raw_name or raw_name.startswith("/"):
        raise RuntimeInstallError(f"Unsafe path in llama.cpp archive: {member.filename!r}")

    is_directory = member.is_dir() or raw_name.endswith("/")
    normalized = raw_name.rstrip("/")
    parts = normalized.split("/") if normalized else []
    if not parts or any(part in {"", ".", ".."} or ":" in part for part in parts):
        raise RuntimeInstallError(f"Unsafe path in llama.cpp archive: {member.filename!r}")

    unix_mode = (member.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(unix_mode)
    if stat.S_ISLNK(unix_mode) or file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
        raise RuntimeInstallError(
            f"Unsupported special file in llama.cpp archive: {member.filename!r}"
        )

    destination = root.joinpath(*parts)
    try:
        destination.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError as exc:
        raise RuntimeInstallError(
            f"Archive entry escapes the llama.cpp install directory: {member.filename!r}"
        ) from exc
    return destination, is_directory


def _extract_archive_safely(archive_path: Path, destination: Path) -> None:
    try:
        with zipfile.ZipFile(archive_path) as archive:
            members = archive.infolist()
            uncompressed_size = sum(member.file_size for member in members if not member.is_dir())
            _require_free_space(
                destination.parent,
                uncompressed_size,
                "extract the llama.cpp runtime",
            )
            for member in members:
                output_path, is_directory = _member_destination(destination, member)
                if is_directory:
                    output_path.mkdir(parents=True, exist_ok=True)
                    continue
                output_path.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member, "r") as source, output_path.open("wb") as output:
                    shutil.copyfileobj(source, output, length=_CHUNK_SIZE)
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise RuntimeInstallError(
            f"Could not extract the llama.cpp archive: {archive_path}"
        ) from exc


def _find_llama_server(directory: Path) -> Path | None:
    if not directory.is_dir():
        return None
    direct = directory / "llama-server.exe"
    if direct.is_file():
        return direct
    candidates = sorted(
        (
            path
            for path in directory.rglob("*")
            if path.is_file() and path.name.casefold() == "llama-server.exe"
        ),
        key=lambda path: str(path).casefold(),
    )
    return candidates[0] if candidates else None


def _ensure_llama_server() -> Path:
    installed_server = _find_llama_server(LLAMA_INSTALL_PATH)
    if installed_server is not None:
        print("llama.cpp server is already installed.")
        return installed_server

    archive_path = _ensure_download(
        LLAMA_URL,
        LLAMA_ARCHIVE_PATH,
        LLAMA_SIZE,
        LLAMA_SHA256,
        "llama.cpp Vulkan runtime",
    )
    LLAMA_INSTALL_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(
            prefix=f".{LLAMA_VERSION}-extract-",
            dir=LLAMA_INSTALL_PATH.parent,
        )
    )
    backup: Path | None = None
    try:
        print(f"Extracting llama.cpp to {LLAMA_INSTALL_PATH}...")
        _extract_archive_safely(archive_path, temporary)
        temporary_server = _find_llama_server(temporary)
        if temporary_server is None:
            raise RuntimeInstallError("The verified llama.cpp archive has no llama-server.exe.")
        server_relative_path = temporary_server.relative_to(temporary)

        if LLAMA_INSTALL_PATH.exists():
            backup = LLAMA_INSTALL_PATH.with_name(
                f".{LLAMA_INSTALL_PATH.name}.old-{uuid.uuid4().hex}"
            )
            os.replace(LLAMA_INSTALL_PATH, backup)
        try:
            os.replace(temporary, LLAMA_INSTALL_PATH)
        except OSError:
            if backup is not None and backup.exists() and not LLAMA_INSTALL_PATH.exists():
                os.replace(backup, LLAMA_INSTALL_PATH)
                backup = None
            raise

        installed_server = LLAMA_INSTALL_PATH / server_relative_path
        if not installed_server.is_file():
            raise RuntimeInstallError("llama-server.exe disappeared after installation.")
        return installed_server
    except OSError as exc:
        raise RuntimeInstallError(
            f"Could not install llama.cpp under {LLAMA_INSTALL_PATH}"
        ) from exc
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
        if backup is not None and backup.exists() and LLAMA_INSTALL_PATH.exists():
            shutil.rmtree(backup, ignore_errors=True)


def ensure_runtime() -> RuntimePaths:
    """Install missing pinned artifacts and return their absolute paths.

    Existing artifacts are reused. Downloads are first written to ``.part``
    files, verified by size and SHA-256, and only then moved into place.
    """

    fallback_model_path = _ensure_download(
        FALLBACK_MODEL_URL,
        FALLBACK_MODEL_PATH,
        FALLBACK_MODEL_SIZE,
        FALLBACK_MODEL_SHA256,
        "Qwen3.5-9B Q4_K_M fallback model",
    )
    model_path = _ensure_download(
        PRIMARY_MODEL_URL,
        PRIMARY_MODEL_PATH,
        PRIMARY_MODEL_SIZE,
        PRIMARY_MODEL_SHA256,
        "Ministral-3-14B-Instruct-2512 Q4_K_M primary model",
    )
    server_path = _ensure_llama_server()
    return RuntimePaths(
        model_path=model_path,
        server_path=server_path,
        fallback_model_path=fallback_model_path,
    )
