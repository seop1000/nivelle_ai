"""Build and verify a deterministic, patchable Nivelle portable ZIP."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import tempfile
import tomllib
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

ROOT_EXCLUDED_DIRECTORIES = {
    ".git",
    ".hg",
    ".nivelle",
    ".nozomi",
    ".svn",
    ".venv",
    "backup",
    "backups",
    "build",
    "data",
    "dist",
    "logs",
    "models",
    "runtime",
    "temp",
    "tmp",
    "update",
    "updates",
    "userdata",
    "venv",
}
CACHE_DIRECTORY_NAMES = {
    ".cache",
    ".mypy_cache",
    ".nox",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    "__pycache__",
}
EXCLUDED_FILE_SUFFIXES = {
    ".bak",
    ".db",
    ".gguf",
    ".key",
    ".log",
    ".p12",
    ".part",
    ".pem",
    ".pfx",
    ".pyc",
    ".pyo",
    ".sqlite",
    ".sqlite3",
    ".tmp",
}
REQUIRED_PORTABLE_PATHS = {
    "VERSION",
    "pyproject.toml",
    "Nivelle-Core.exe",
    "Nivelle-Link.exe",
    "Nivelle-Local.exe",
    "Nivelle-Updater.exe",
    "Nivelle-Update.cmd",
    "Nivelle-Rollback.cmd",
    "config/examples/server.yaml",
    "scripts/apply_update.ps1",
    "scripts/bootstrap_python.ps1",
    "scripts/build_executables.ps1",
    "scripts/build_update.ps1",
    "scripts/nivelle_executable_launcher.py",
    "scripts/rollback_update.ps1",
    "scripts/run_locked.ps1",
    "scripts/update_from_github.ps1",
    "scripts/verify_update.ps1",
}
REQUIRED_PORTABLE_PATH_ALTERNATIVES = (
    ("nivelle.py", "nozomi.py"),
    ("nivelle_runtime.py", "nozomi_runtime.py"),
    ("apps/server/nivelle_core/app.py", "apps/server/nozomi_server/app.py"),
    ("apps/client/nivelle_link/main.py", "apps/client/nozomi_client/main.py"),
    ("packages/nivelle_protocol/version.py", "packages/nozomi_protocol/version.py"),
    ("packages/nivelle_protocol/persona.py", "packages/nozomi_protocol/persona.py"),
)
LEGACY_BINARY_FILENAMES = {
    "nozomi-server.exe",
    "nozomi-client.exe",
    "nozomi-local.exe",
    "nozomi-updater.exe",
}
WINDOWS_RESERVED_NAMES = {
    "aux",
    "con",
    "nul",
    "prn",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}
FIXED_ZIP_TIME = (2000, 1, 1, 0, 0, 0)
VERSION_PATTERN = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$")


class PortableBuildError(RuntimeError):
    """Raised when a portable package cannot be produced safely."""


@dataclass(frozen=True, slots=True)
class SourceFile:
    relative_path: str
    full_path: Path


def safe_relative_path(value: str) -> str:
    """Return a canonical portable path or reject Windows/path traversal hazards."""

    normalized = value.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if not normalized or normalized.startswith("/") or "\0" in normalized:
        raise PortableBuildError(f"Unsafe portable path: {value!r}")
    if re.match(r"^[A-Za-z]:", normalized):
        raise PortableBuildError(f"Unsafe portable path: {value!r}")

    segments = normalized.split("/")
    for segment in segments:
        if (
            not segment
            or segment in {".", ".."}
            or segment.endswith((" ", "."))
            or any(ord(character) < 32 for character in segment)
            or any(character in '<>:"|?*' for character in segment)
            or segment.split(".", 1)[0].casefold() in WINDOWS_RESERVED_NAMES
        ):
            raise PortableBuildError(f"Unsafe portable path: {value!r}")
    return "/".join(segments)


def is_excluded_directory(relative_path: str) -> bool:
    segments = PurePosixPath(relative_path).parts
    folded = tuple(segment.casefold() for segment in segments)
    if not folded:
        return False
    if folded[0] in ROOT_EXCLUDED_DIRECTORIES:
        return True
    if folded[0].startswith((".tmp.", ".venv.broken-")):
        return True
    if folded[0].startswith(
        (
            "nivelle-portable-",
            "nivelle-windows-",
            "nivelle-update-",
            "nozomi-portable-",
            "nozomi-windows-",
            "nozomi-update-",
        )
    ):
        return True
    if any(
        segment in CACHE_DIRECTORY_NAMES or segment.startswith(".venv.broken-")
        for segment in folded
    ):
        return True
    return len(folded) >= 2 and folded[0] == "config" and folded[1] != "examples"


def is_excluded_file(relative_path: str) -> bool:
    path = PurePosixPath(relative_path)
    folded_parts = tuple(part.casefold() for part in path.parts)
    if len(folded_parts) > 1 and is_excluded_directory("/".join(path.parts[:-1])):
        return True
    if folded_parts and folded_parts[0] == "config":
        if len(folded_parts) < 3 or folded_parts[1] != "examples":
            return True

    leaf = path.name.casefold()
    if len(folded_parts) == 1 and leaf in LEGACY_BINARY_FILENAMES:
        return True
    if leaf in {".env", ".coverage", ".ds_store", "connections.yaml"}:
        return True
    if leaf.startswith(".env.") and leaf != ".env.example":
        return True
    if leaf.endswith((".db-wal", ".db-shm", ".db-journal", ".exe.new")):
        return True
    return any(leaf.endswith(suffix) for suffix in EXCLUDED_FILE_SUFFIXES)


def iter_source_files(root: Path) -> list[SourceFile]:
    root = root.resolve(strict=True)
    files: list[SourceFile] = []
    pending = [root]
    seen: set[str] = set()

    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in sorted(entries, key=lambda item: item.name.casefold(), reverse=True):
                full_path = Path(entry.path)
                relative = safe_relative_path(full_path.relative_to(root).as_posix())
                if entry.is_dir(follow_symlinks=False):
                    if is_excluded_directory(relative):
                        continue
                    if entry.is_symlink():
                        raise PortableBuildError(
                            f"Refusing to package a symbolic-link directory: {relative}"
                        )
                    pending.append(full_path)
                    continue
                if is_excluded_file(relative):
                    continue
                if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                    raise PortableBuildError(f"Refusing to package a special file: {relative}")
                folded = relative.casefold()
                if folded in seen:
                    raise PortableBuildError(
                        f"Duplicate case-insensitive portable path: {relative}"
                    )
                seen.add(folded)
                files.append(SourceFile(relative, full_path))

    return sorted(files, key=lambda item: item.relative_path)


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def project_version(root: Path) -> str:
    version_path = root / "VERSION"
    try:
        version = version_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise PortableBuildError(f"Could not read {version_path}") from exc
    if not VERSION_PATTERN.fullmatch(version):
        raise PortableBuildError(f"Unsafe VERSION value: {version!r}")

    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    project_dynamic = pyproject.get("project", {}).get("dynamic", [])
    hatch_version = pyproject.get("tool", {}).get("hatch", {}).get("version", {})
    version_sources = (
        root / "packages/nivelle_protocol/version.py",
        root / "packages/nozomi_protocol/version.py",
    )
    app_version_path = next((path for path in version_sources if path.is_file()), None)
    if app_version_path is None:
        raise PortableBuildError("Nivelle protocol version source was not found")
    app_version_source = app_version_path.read_text(encoding="utf-8")
    if "version" not in project_dynamic or hatch_version.get("path") != "VERSION":
        raise PortableBuildError("pyproject.toml must derive its version from VERSION")
    if not re.search(r"^APP_VERSION\s*=\s*_load_app_version\(\)", app_version_source, re.M):
        raise PortableBuildError("APP_VERSION must derive from the canonical VERSION source")
    return version


def _write_zip(output: Path, source_files: Iterable[SourceFile]) -> None:
    with zipfile.ZipFile(
        output,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        allowZip64=True,
    ) as archive:
        for source in source_files:
            info = zipfile.ZipInfo(source.relative_path, FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 0
            info.external_attr = 0x20
            archive.writestr(
                info,
                source.full_path.read_bytes(),
                compress_type=zipfile.ZIP_DEFLATED,
                compresslevel=9,
            )


def verify_portable(archive_path: Path, root: Path, source_files: list[SourceFile]) -> None:
    expected = {source.relative_path: source for source in source_files}
    folded_seen: set[str] = set()
    with zipfile.ZipFile(archive_path, "r") as archive:
        names: list[str] = []
        for entry in archive.infolist():
            if entry.is_dir():
                raise PortableBuildError(f"Unexpected directory ZIP entry: {entry.filename}")
            relative = safe_relative_path(entry.filename)
            folded = relative.casefold()
            if folded in folded_seen:
                raise PortableBuildError(f"Duplicate ZIP entry: {relative}")
            folded_seen.add(folded)
            if is_excluded_file(relative):
                raise PortableBuildError(f"Protected file entered portable ZIP: {relative}")
            source = expected.get(relative)
            if source is None:
                raise PortableBuildError(f"Unexpected portable ZIP entry: {relative}")
            payload = archive.read(entry)
            if sha256_bytes(payload) != sha256_file(source.full_path):
                raise PortableBuildError(f"Portable ZIP payload mismatch: {relative}")
            names.append(relative)

    if set(names) != set(expected):
        missing = sorted(set(expected) - set(names))
        raise PortableBuildError(f"Portable ZIP is missing source files: {missing}")
    missing_required = sorted(REQUIRED_PORTABLE_PATHS - set(names))
    if missing_required:
        raise PortableBuildError(f"Portable ZIP is missing required files: {missing_required}")
    for alternatives in REQUIRED_PORTABLE_PATH_ALTERNATIVES:
        if not any(path in names for path in alternatives):
            raise PortableBuildError(
                "Portable ZIP is missing a required compatibility path: "
                + " or ".join(alternatives)
            )


def extract_portable(archive_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    destination = destination.resolve(strict=True)
    with zipfile.ZipFile(archive_path, "r") as archive:
        seen: set[str] = set()
        for entry in archive.infolist():
            relative = safe_relative_path(entry.filename)
            folded = relative.casefold()
            if folded in seen or entry.is_dir():
                raise PortableBuildError(f"Unsafe extraction entry: {relative}")
            seen.add(folded)
            target = (destination / PurePosixPath(relative)).resolve()
            if destination not in target.parents:
                raise PortableBuildError(f"ZIP entry escaped extraction root: {relative}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(entry, "r") as source, target.open("xb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)


def build_portable(
    root: Path,
    output: Path,
    *,
    force: bool = False,
    extract_to: Path | None = None,
) -> tuple[str, int]:
    root = root.resolve(strict=True)
    project_version(root)
    source_files = iter_source_files(root)

    output = output.resolve()
    sidecar = output.with_name(f"{output.name}.sha256")
    output.parent.mkdir(parents=True, exist_ok=True)
    if not force and (output.exists() or sidecar.exists()):
        raise PortableBuildError(f"Portable output already exists; use --force: {output}")

    temporary_fd, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    os.close(temporary_fd)
    temporary = Path(temporary_name)
    try:
        _write_zip(temporary, source_files)
        verify_portable(temporary, root, source_files)
        os.replace(temporary, output)
        digest = sha256_file(output)
        sidecar_temporary = sidecar.with_name(f".{sidecar.name}.{os.getpid()}.tmp")
        sidecar_temporary.write_text(f"{digest} *{output.name}\n", encoding="utf-8", newline="\n")
        os.replace(sidecar_temporary, sidecar)
        if extract_to is not None:
            extract_portable(output, extract_to)
        return digest, output.stat().st_size
    finally:
        temporary.unlink(missing_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--extract-to", type=Path)
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        digest, size = build_portable(
            args.project_root,
            args.output,
            force=args.force,
            extract_to=args.extract_to,
        )
    except (OSError, zipfile.BadZipFile, PortableBuildError) as exc:
        print(f"Portable build failed: {exc}")
        return 1
    print(f"Portable ZIP: {args.output}")
    print(f"Size: {size} bytes")
    print(f"SHA-256: {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
