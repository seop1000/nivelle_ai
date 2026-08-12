from __future__ import annotations

import base64
import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .errors import PathValidationError
from .models import AgentPolicy

_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "CLOCK$",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
_SENSITIVE_EXACT_NAMES = {
    ".env",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "login data",
    "local state",
    "cookies",
    "credentials",
    "credential",
    "auth.json",
    "authentication.json",
    "token.json",
    "tokens.json",
    "pairing.json",
    "pairing-secret",
    "pairing_secret",
}
_SENSITIVE_DIR_NAMES = {
    ".ssh",
    ".gnupg",
    ".aws",
    ".azure",
    "password managers",
}
_SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".kdbx"}
_SENSITIVE_SUBSTRINGS = {
    "api_token",
    "api-token",
    "auth_token",
    "auth-token",
    "token_cache",
    "token-cache",
    "pairing_secret",
    "pairing-secret",
    "nivelle_auth",
    # Protect credentials created by the 0.3.1 client after an in-place upgrade.
    "nozomi_auth",
}
_CONTROL_EXCEPTIONS = {"\t", "\n", "\r"}


@dataclass(frozen=True)
class PathIdentity:
    device: int
    inode: int
    size: int
    modified_ns: int


@dataclass(frozen=True)
class ValidatedPath:
    path: Path
    root_id: str
    root_path: Path
    relative_path: str
    identity: PathIdentity | None


def sanitize_display_text(value: str) -> str:
    return "".join(
        character
        for character in value
        if character in _CONTROL_EXCEPTIONS or unicodedata.category(character) != "Cc"
    )


def is_sensitive_path(path: Path) -> bool:
    parts = [part.casefold().rstrip(" .") for part in path.parts]
    for part in parts:
        if part in _SENSITIVE_DIR_NAMES or part in _SENSITIVE_EXACT_NAMES:
            return True
        if any(fragment in part for fragment in _SENSITIVE_SUBSTRINGS):
            return True
        if Path(part).suffix.casefold() in _SENSITIVE_SUFFIXES:
            return True
        if part.startswith(".env."):
            return True
    return False


def is_hidden_or_system(path: Path) -> tuple[bool, bool]:
    hidden = any(part.startswith(".") and part not in {".", ".."} for part in path.parts)
    system = False
    try:
        attributes = path.stat(follow_symlinks=False).st_file_attributes
    except (AttributeError, FileNotFoundError, OSError):
        attributes = 0
    hidden = hidden or bool(attributes & stat.FILE_ATTRIBUTE_HIDDEN)
    system = bool(attributes & stat.FILE_ATTRIBUTE_SYSTEM)
    return hidden, system


def is_reparse_point(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = path.stat(follow_symlinks=False).st_file_attributes
    except (AttributeError, FileNotFoundError, OSError):
        return False
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _identity(path: Path) -> PathIdentity:
    info = path.stat()
    return PathIdentity(
        device=info.st_dev,
        inode=info.st_ino,
        size=info.st_size,
        modified_ns=info.st_mtime_ns,
    )


def _casefold_path(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path))).casefold()


def _contains(root: Path, candidate: Path) -> bool:
    root_text = _casefold_path(root)
    candidate_text = _casefold_path(candidate)
    try:
        return os.path.commonpath([root_text, candidate_text]) == root_text
    except ValueError:
        return False


def _validate_raw_windows_path(raw_path: str, *, allow_network_paths: bool) -> str:
    normalized = unicodedata.normalize("NFKC", raw_path)
    if not normalized:
        raise PathValidationError("validation_failed", "The path is empty.")
    slash_normalized = normalized.replace("/", "\\")
    lowered = slash_normalized.casefold()
    if lowered.startswith("\\\\.\\"):
        raise PathValidationError("path_not_allowed", "Windows device paths are not allowed.")
    if lowered.startswith("\\\\?\\"):
        raise PathValidationError(
            "path_not_allowed", "Model-provided extended Windows paths are not allowed."
        )
    if slash_normalized.startswith("\\\\") and not allow_network_paths:
        raise PathValidationError("path_not_allowed", "Network paths are disabled by local policy.")

    components = [item for item in re.split(r"[\\/]", normalized) if item]
    if ".." in components:
        raise PathValidationError("path_not_allowed", "Parent traversal is not allowed.")

    drive_prefix = bool(re.match(r"^[A-Za-z]:[\\/]", normalized))
    colon_scan = normalized[2:] if drive_prefix else normalized
    if ":" in colon_scan:
        raise PathValidationError(
            "path_not_allowed", "Windows alternate data streams are not allowed."
        )

    for component in components:
        cleaned = component.rstrip(" .")
        stem = cleaned.split(".", 1)[0].upper()
        if stem in _RESERVED_NAMES:
            raise PathValidationError("path_not_allowed", "A reserved Windows name is not allowed.")
    return slash_normalized


class WindowsPathValidator:
    def __init__(self, policy: AgentPolicy) -> None:
        self.policy = policy
        self._roots_cache: dict[str, Path] | None = None

    def _configured_roots(self) -> dict[str, Path]:
        if self._roots_cache is not None:
            return self._roots_cache
        roots: dict[str, Path] = {}
        for root_id, root in self.policy.filesystem_roots.items():
            raw = _validate_raw_windows_path(
                str(root.path), allow_network_paths=self.policy.allow_network_paths
            )
            candidate = Path(raw)
            if not candidate.is_absolute():
                raise PathValidationError(
                    "path_not_allowed", f"Configured root {root_id!r} is not absolute."
                )
            try:
                canonical_root = candidate.resolve(strict=True)
            except OSError as exc:
                raise PathValidationError(
                    "target_not_found", f"Configured root {root_id!r} is unavailable."
                ) from exc
            if not canonical_root.is_dir():
                raise PathValidationError(
                    "path_not_allowed", f"Configured root {root_id!r} is not a folder."
                )
            roots[root_id] = canonical_root
        self._roots_cache = roots
        return roots

    def _reject_reparse_components(self, candidate: Path) -> None:
        if self.policy.allow_reparse_points:
            return
        current = Path(candidate.anchor)
        for component in candidate.parts[1:]:
            current /= component
            if current.exists() and is_reparse_point(current):
                raise PathValidationError(
                    "path_not_allowed", "Symbolic links, junctions, and reparse points are disabled."
                )

    def validate(
        self,
        raw_path: str | Path,
        *,
        root_id: str | None = None,
        require_exists: bool = True,
        expected_type: Literal["file", "directory", "any"] = "any",
        max_size: int | None = None,
        reject_sensitive: bool = True,
    ) -> ValidatedPath:
        normalized = _validate_raw_windows_path(
            str(raw_path), allow_network_paths=self.policy.allow_network_paths
        )
        candidate = Path(normalized)
        if not candidate.is_absolute():
            raise PathValidationError("path_not_allowed", "Relative paths are not allowed.")
        self._reject_reparse_components(candidate)

        try:
            canonical = candidate.resolve(strict=require_exists)
        except OSError as exc:
            raise PathValidationError("target_not_found", "The requested target does not exist.") from exc
        if require_exists and not canonical.exists():
            raise PathValidationError("target_not_found", "The requested target does not exist.")

        roots = self._configured_roots()
        if root_id is not None:
            root = roots.get(root_id)
            if root is None:
                raise PathValidationError("path_not_allowed", "The filesystem root is not approved.")
            selected_root_id = root_id
        else:
            matches = [
                (candidate_root_id, root)
                for candidate_root_id, root in roots.items()
                if _contains(root, canonical)
            ]
            if not matches:
                raise PathValidationError("path_not_allowed", "The path is outside approved roots.")
            selected_root_id, root = max(matches, key=lambda item: len(str(item[1])))

        if not _contains(root, canonical):
            raise PathValidationError("path_not_allowed", "The path is outside the approved root.")
        for denied in self.policy.denied_paths:
            denied_normalized = _validate_raw_windows_path(
                str(denied), allow_network_paths=self.policy.allow_network_paths
            )
            denied_candidate = Path(denied_normalized)
            if not denied_candidate.is_absolute():
                raise PathValidationError(
                    "path_not_allowed", "A configured denied path is not absolute."
                )
            denied_path = denied_candidate.resolve(strict=False)
            if _contains(denied_path, canonical):
                raise PathValidationError("path_not_allowed", "The path is denied by local policy.")

        if reject_sensitive and is_sensitive_path(canonical):
            raise PathValidationError("sensitive_path", "The requested path is sensitive.")
        relative_components = canonical.relative_to(root).parts
        current_component = root
        for component in relative_components:
            current_component /= component
            hidden, system = is_hidden_or_system(current_component)
            if hidden and not self.policy.allow_hidden_files:
                raise PathValidationError(
                    "path_not_allowed", "Hidden paths are disabled by local policy."
                )
            if system and not self.policy.allow_system_files:
                raise PathValidationError(
                    "path_not_allowed", "System paths are disabled by local policy."
                )

        info: PathIdentity | None
        if require_exists:
            if expected_type == "file" and not canonical.is_file():
                raise PathValidationError("target_not_found", "The requested target is not a file.")
            if expected_type == "directory" and not canonical.is_dir():
                raise PathValidationError("target_not_found", "The requested target is not a folder.")
            info = _identity(canonical)
            if max_size is not None and info.size > max_size:
                raise PathValidationError("result_too_large", "The requested file is too large.")
        else:
            info = _identity(canonical) if canonical.exists() else None

        return ValidatedPath(
            path=canonical,
            root_id=selected_root_id,
            root_path=root,
            relative_path=str(canonical.relative_to(root)),
            identity=info,
        )

    def revalidate(
        self,
        validated: ValidatedPath,
        *,
        expected_type: Literal["file", "directory", "any"] = "any",
        max_size: int | None = None,
        reject_sensitive: bool = True,
    ) -> ValidatedPath:
        checked = self.validate(
            validated.path,
            root_id=validated.root_id,
            require_exists=True,
            expected_type=expected_type,
            max_size=max_size,
            reject_sensitive=reject_sensitive,
        )
        if validated.identity is not None and checked.identity != validated.identity:
            raise PathValidationError(
                "path_not_allowed", "The target changed after validation; access was cancelled."
            )
        return checked

    @staticmethod
    def make_path_ref(root_id: str, relative_path: str) -> str:
        encoded = base64.urlsafe_b64encode(relative_path.encode("utf-8")).decode("ascii")
        return f"{root_id}:{encoded.rstrip('=')}"

    def resolve_path_ref(self, path_ref: str) -> tuple[str, Path]:
        try:
            root_id, encoded = path_ref.split(":", 1)
            padding = "=" * (-len(encoded) % 4)
            relative = base64.b64decode(encoded + padding, altchars=b"-_", validate=True).decode(
                "utf-8"
            )
        except (ValueError, UnicodeDecodeError) as exc:
            raise PathValidationError("validation_failed", "The path reference is invalid.") from exc
        if not root_id or Path(relative).is_absolute():
            raise PathValidationError("validation_failed", "The path reference is invalid.")
        root = self.policy.filesystem_roots.get(root_id)
        if root is None:
            raise PathValidationError("path_not_allowed", "The filesystem root is not approved.")
        return root_id, Path(root.path) / relative
