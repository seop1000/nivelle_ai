from __future__ import annotations

from pathlib import Path

import pytest
from nivelle_link.agent import IdempotencyCache, IdempotencyError


def test_pending_side_effect_is_never_repeated_after_uncertain_failure(tmp_path: Path) -> None:
    cache = IdempotencyCache(tmp_path / "idempotency.json")
    calls = 0

    def uncertain_operation() -> dict[str, object]:
        nonlocal calls
        calls += 1
        raise RuntimeError("failure after an uncertain boundary")

    with pytest.raises(RuntimeError):
        cache.execute_once(
            idempotency_key="one",
            tool_name="open_application",
            arguments={"application_id": "editor"},
            operation=uncertain_operation,
        )
    with pytest.raises(IdempotencyError) as duplicate:
        cache.execute_once(
            idempotency_key="one",
            tool_name="open_application",
            arguments={"application_id": "editor"},
            operation=uncertain_operation,
        )

    assert duplicate.value.code == "duplicate_request"
    assert calls == 1
    assert "one" not in (tmp_path / "idempotency.json").read_text(encoding="utf-8")


def test_completed_result_replays_and_key_reuse_with_other_arguments_fails(
    tmp_path: Path,
) -> None:
    cache = IdempotencyCache(tmp_path / "idempotency.json")
    calls = 0

    def operation() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"process_id": 42}

    first, replayed = cache.execute_once(
        idempotency_key="one",
        tool_name="open_application",
        arguments={"application_id": "editor"},
        operation=operation,
    )
    second, second_replayed = cache.execute_once(
        idempotency_key="one",
        tool_name="open_application",
        arguments={"application_id": "editor"},
        operation=operation,
    )

    assert first == second == {"process_id": 42}
    assert replayed is False and second_replayed is True
    assert calls == 1
    with pytest.raises(IdempotencyError):
        cache.execute_once(
            idempotency_key="one",
            tool_name="open_application",
            arguments={"application_id": "other"},
            operation=operation,
        )
