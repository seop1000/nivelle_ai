from __future__ import annotations

import os
from pathlib import Path

import pytest
from nivelle_link.agent import AgentPolicy, FilesystemRoot, PathValidationError
from nivelle_link.agent.path_security import WindowsPathValidator


def policy_for(root: Path, **updates: object) -> AgentPolicy:
    policy = AgentPolicy(
        filesystem_roots={
            "workspace": FilesystemRoot(
                display_name="Workspace",
                path=root,
                allow_search=True,
                allow_read=True,
                allow_open_folder=True,
            )
        }
    )
    return policy.model_copy(update=updates)


def test_allowed_unicode_mixed_separator_and_case_insensitive_paths(tmp_path: Path) -> None:
    root = tmp_path / "자료"
    root.mkdir()
    target = root / "니벨.txt"
    target.write_text("안녕하세요", encoding="utf-8")
    validator = WindowsPathValidator(policy_for(root))

    mixed = str(target).replace("\\", "/")
    assert validator.validate(mixed, expected_type="file").path == target.resolve()
    assert validator.validate(str(target).swapcase(), expected_type="file").path == target.resolve()


def test_outside_parent_traversal_relative_and_missing_are_denied(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    validator = WindowsPathValidator(policy_for(root))

    with pytest.raises(PathValidationError, match="outside approved roots"):
        validator.validate(outside)
    with pytest.raises(PathValidationError, match="Parent traversal"):
        validator.validate(f"{root}\\..//outside.txt")
    with pytest.raises(PathValidationError, match="Relative"):
        validator.validate("relative.txt")
    with pytest.raises(PathValidationError, match="does not exist"):
        validator.validate(root / "missing.txt")


@pytest.mark.parametrize(
    "malicious",
    [
        r"\\server\share\file.txt",
        r"\\.\PhysicalDrive0",
        r"\\?\C:\Windows\notepad.exe",
        r"C:\safe.txt:secret",
        r"C:\temp\CON.txt",
        r"C:\temp\LPT9.log",
    ],
)
def test_unc_device_extended_ads_and_reserved_paths_are_denied(
    tmp_path: Path, malicious: str
) -> None:
    validator = WindowsPathValidator(policy_for(tmp_path))
    with pytest.raises(PathValidationError):
        validator.validate(malicious, require_exists=False)


def test_hidden_sensitive_and_oversized_files_are_denied(tmp_path: Path) -> None:
    root = tmp_path / "root"
    hidden_directory = root / ".hidden"
    hidden_directory.mkdir(parents=True)
    hidden = hidden_directory / "visible-name.txt"
    hidden.write_text("hidden", encoding="utf-8")
    sensitive = root / ".env"
    sensitive.write_text("TOKEN=value", encoding="utf-8")
    oversized = root / "large.txt"
    oversized.write_bytes(b"x" * 20)
    validator = WindowsPathValidator(policy_for(root))

    with pytest.raises(PathValidationError, match="Hidden"):
        validator.validate(hidden, expected_type="file")
    with pytest.raises(PathValidationError) as sensitive_error:
        validator.validate(sensitive, expected_type="file")
    assert sensitive_error.value.code == "sensitive_path"
    with pytest.raises(PathValidationError) as size_error:
        validator.validate(oversized, expected_type="file", max_size=10)
    assert size_error.value.code == "result_too_large"


def test_symlink_or_junction_escape_is_denied(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    link = root / "junction"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Creating a Windows reparse-point link is unavailable: {exc}")

    validator = WindowsPathValidator(policy_for(root))
    with pytest.raises(PathValidationError, match="reparse"):
        validator.validate(link / "secret.txt", expected_type="file")


def test_reparse_point_guard_is_enforced_without_following_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    junction = root / "junction"
    junction.mkdir(parents=True)
    target = junction / "target.txt"
    target.write_text("target", encoding="utf-8")
    import nivelle_link.agent.path_security as path_security

    original = path_security.is_reparse_point
    monkeypatch.setattr(
        path_security,
        "is_reparse_point",
        lambda path: path == junction or original(path),
    )

    with pytest.raises(PathValidationError, match="reparse"):
        WindowsPathValidator(policy_for(root)).validate(target, expected_type="file")


def test_revalidation_detects_time_of_check_change(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    target = root / "document.txt"
    target.write_text("one", encoding="utf-8")
    validator = WindowsPathValidator(policy_for(root))
    validated = validator.validate(target, expected_type="file")

    target.write_text("different size", encoding="utf-8")
    with pytest.raises(PathValidationError, match="changed after validation"):
        validator.revalidate(validated, expected_type="file")


def test_path_references_are_stable_and_revalidated(tmp_path: Path) -> None:
    root = tmp_path / "root"
    nested = root / "folder"
    nested.mkdir(parents=True)
    target = nested / "one.txt"
    target.write_text("one", encoding="utf-8")
    validator = WindowsPathValidator(policy_for(root))
    reference = validator.make_path_ref("workspace", r"folder\one.txt")

    root_id, resolved = validator.resolve_path_ref(reference)
    assert root_id == "workspace"
    assert validator.validate(resolved, root_id=root_id).path == target.resolve()
    assert reference == validator.make_path_ref("workspace", r"folder\one.txt")


@pytest.mark.skipif(os.name != "nt", reason="Windows system attribute test")
def test_system_file_attribute_is_denied(tmp_path: Path) -> None:
    import ctypes

    root = tmp_path / "root"
    root.mkdir()
    target = root / "system.txt"
    target.write_text("system", encoding="utf-8")
    set_attributes = ctypes.windll.kernel32.SetFileAttributesW
    if not set_attributes(str(target), 0x4):
        pytest.skip("Could not set the Windows system attribute")
    try:
        with pytest.raises(PathValidationError, match="System"):
            WindowsPathValidator(policy_for(root)).validate(target)
    finally:
        set_attributes(str(target), 0x80)
