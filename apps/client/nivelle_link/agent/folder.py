from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from nivelle_protocol.tools import OpenFolderArguments

from .errors import AgentError
from .models import AgentPolicy
from .path_security import WindowsPathValidator
from .result_utils import untrusted_result


def default_folder_launcher(path: Path) -> None:
    if os.name != "nt":
        raise AgentError("execution_failed", "Folder launch is supported only on Windows.")
    startfile = os.startfile
    startfile(str(path))


def open_folder(
    arguments_payload: dict[str, Any],
    *,
    policy: AgentPolicy,
    launcher: Callable[[Path], None] = default_folder_launcher,
) -> dict[str, Any]:
    arguments = OpenFolderArguments.model_validate(arguments_payload)
    validator = WindowsPathValidator(policy)
    if arguments.path_ref is not None:
        root_id, target = validator.resolve_path_ref(arguments.path_ref)
    else:
        if not policy.allow_direct_paths:
            raise AgentError("permission_denied", "Direct filesystem paths are disabled locally.")
        root_id, target = None, Path(arguments.path or "")
    validated = validator.validate(
        target, root_id=root_id, expected_type="directory", reject_sensitive=True
    )
    root = policy.filesystem_roots[validated.root_id]
    if not root.allow_open_folder:
        raise AgentError("permission_denied", "Opening folders is disabled for this root.")
    checked = validator.revalidate(validated, expected_type="directory", reject_sensitive=True)
    path_ref = validator.make_path_ref(checked.root_id, checked.relative_path)
    if len(path_ref) > 512:
        raise AgentError("result_too_large", "The folder path reference is too long to return safely.")
    launcher(checked.path)
    content = {
        "root_id": checked.root_id,
        "path_ref": path_ref,
        "relative_path": checked.relative_path,
    }
    return untrusted_result("open_folder", content=content, returned_size=len(str(content)))
