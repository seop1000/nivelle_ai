from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path
from types import TracebackType
from typing import Any, cast

import pytest
from nivelle_link.agent.models import AgentPolicy, FilesystemRoot
from nivelle_link.agent.search import search_files


def search_policy(root: Path, *, allow_reparse_points: bool = False) -> AgentPolicy:
    return AgentPolicy(
        agent_enabled=True,
        enabled_tools={"search_files"},
        filesystem_roots={
            "workspace": FilesystemRoot(
                display_name="Workspace",
                path=root,
                allow_search=True,
            )
        },
        allow_reparse_points=allow_reparse_points,
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows junction test")
def test_search_never_crosses_junction_outside_approved_root(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "outside-secret-name.txt").write_text("secret", encoding="utf-8")
    junction = workspace / "allowed-looking-folder"
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )
    if completed.returncode != 0:
        pytest.skip("This Windows account cannot create a test junction")

    try:
        result = search_files(
            {
                "query": "outside-secret",
                "root_id": "workspace",
                "max_depth": 2,
            },
            policy=search_policy(workspace, allow_reparse_points=True),
        )
        assert result["content"]["items"] == []
    finally:
        junction.rmdir()


def test_search_checks_cancellation_while_scandir_is_streaming(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for index in range(100):
        (workspace / f"item-{index:03}.txt").write_text("x", encoding="utf-8")

    real_scandir = os.scandir
    yielded = 0

    class CountingScandir:
        def __init__(self, path: str | bytes | os.PathLike[str] | os.PathLike[bytes]) -> None:
            self._iterator = real_scandir(path)

        def __enter__(self) -> CountingScandir:
            self._iterator.__enter__()
            return self

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc_value: BaseException | None,
            traceback: TracebackType | None,
        ) -> bool | None:
            return self._iterator.__exit__(exc_type, exc_value, traceback)

        def __iter__(self) -> CountingScandir:
            return self

        def __next__(self) -> os.DirEntry[Any]:
            nonlocal yielded
            entry = next(self._iterator)
            yielded += 1
            return cast(os.DirEntry[Any], entry)

    class CancelAfterFive(threading.Event):
        def is_set(self) -> bool:
            return yielded >= 5

    monkeypatch.setattr("nivelle_link.agent.search.os.scandir", CountingScandir)
    with pytest.raises(Exception, match="cancelled"):
        search_files(
            {"query": "item", "root_id": "workspace"},
            policy=search_policy(workspace),
            cancellation=CancelAfterFive(),
        )

    assert yielded == 5
