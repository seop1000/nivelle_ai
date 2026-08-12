from pathlib import Path

import pytest
from nivelle_core.database import Database
from nivelle_core.memory_repository import DuplicateMemoryError, MemoryRepository
from nivelle_core.memory_retriever import (
    MemoryRetriever,
    normalize_memory_content,
)
from nivelle_protocol.memory import MemoryCreate, MemoryRetrievalSettings, MemoryUpdate


async def _repository(tmp_path: Path) -> tuple[Database, MemoryRepository]:
    database = Database(tmp_path / "nivelle.db")
    await database.initialize()
    return database, MemoryRepository(database)


def test_memory_content_normalization_is_nfkc_whitespace_and_punctuation_stable() -> None:
    assert normalize_memory_content("  Ｎｉｖｅｌｌｅ，  회색！ ") == "nivelle 회색"
    assert normalize_memory_content("Nivelle: 회색") == normalize_memory_content(
        "nivelle  회색."
    )


@pytest.mark.asyncio
async def test_exact_duplicate_protection_and_same_record_update(tmp_path: Path) -> None:
    database, repository = await _repository(tmp_path)
    original = await repository.create(MemoryCreate(content="Nivelle: 회색 기억"))

    with pytest.raises(DuplicateMemoryError) as duplicate:
        await repository.create(MemoryCreate(content="  nivelle， 회색 기억!  "))
    assert duplicate.value.existing_memory_id == original.id

    similar = await repository.create(MemoryCreate(content="Nivelle 회색 기억을 사용한다"))
    assert similar.id != original.id

    updated = await repository.update(original.id, MemoryUpdate(content="NIVELLE, 회색 기억."))
    assert updated is not None and updated.id == original.id
    revisions = await database.fetchall(
        "SELECT old_content,new_content FROM memory_revisions WHERE memory_id=?", (original.id,)
    )
    assert [(row["old_content"], row["new_content"]) for row in revisions] == [
        ("Nivelle: 회색 기억", "NIVELLE, 회색 기억.")
    ]


@pytest.mark.asyncio
async def test_update_reindexes_latest_value_and_delete_releases_duplicate_key(
    tmp_path: Path,
) -> None:
    _, repository = await _repository(tmp_path)
    memory = await repository.create(MemoryCreate(content="테스트 강조색은 보라색이다"))
    updated = await repository.update(
        memory.id, MemoryUpdate(content="테스트 강조색은 회색이다")
    )
    assert updated is not None and updated.id == memory.id
    assert memory.id not in {item.id for item in await repository.search("보라색")}
    assert [item.id for item in await repository.search("회색")] == [memory.id]

    assert await repository.delete(memory.id)
    replacement = await repository.create(MemoryCreate(content="테스트 강조색은 회색이다"))
    assert replacement.id != memory.id


@pytest.mark.asyncio
async def test_korean_prefix_and_substring_searches_server_memory(tmp_path: Path) -> None:
    database, repository = await _repository(tmp_path)
    nickname = await repository.create(
        MemoryCreate(content="사용자의 기본 호칭은 히냥이이다.", priority=100)
    )
    hardware = await repository.create(
        MemoryCreate(
            content=(
                "Nivelle Core PC는 Windows 11 Home이 설치된 ROG Xbox Ally X이며 "
                "공유 메모리 24GB 중 시스템 RAM 16GB와 GPU 예약 메모리 8GB로 설정되어 있다."
            ),
            category="project",
            priority=40,
        )
    )

    assert nickname.id in {item.id for item in await repository.search("히냥이")}
    for query in (
        "서버 메모리",
        "RAM 16GB",
        "GPU 8GB",
        "메모리 배분",
        "시스템램",
        "Ally X 메모리",
    ):
        assert hardware.id in {item.id for item in await repository.search(query)}, query
    # The deployment may omit the optional tokenizer; either path must work.
    assert isinstance(database.trigram_available, bool)


@pytest.mark.asyncio
async def test_retriever_relevance_dominates_unrelated_priority_and_reports_reasons(
    tmp_path: Path,
) -> None:
    _, repository = await _repository(tmp_path)
    nickname = await repository.create(
        MemoryCreate(content="사용자의 기본 호칭은 히냥이이다", priority=100)
    )
    hardware = await repository.create(
        MemoryCreate(
            content="Nivelle Core PC의 시스템 RAM은 16GB이고 GPU 예약 메모리는 8GB이다",
            category="project",
            priority=20,
        )
    )
    inactive = await repository.create(
        MemoryCreate(content="서버 RAM은 64GB이다", active=False, priority=100)
    )
    settings = MemoryRetrievalSettings(top_k=2, candidate_limit=10)
    result = await MemoryRetriever(repository, settings).retrieve(
        "서버 PC의 시스템 RAM과 GPU 예약 메모리는 각각 얼마야?"
    )

    assert result.backend == "sqlite_hybrid"
    assert result.selected[0].memory_id == hardware.id
    assert nickname.id not in {item.memory_id for item in result.selected}
    reasons = {item.memory_id: item.reason for item in result.rejected}
    assert reasons[nickname.id] == "low_relevance"
    assert reasons[inactive.id] == "inactive"
    selected = result.selected[0]
    expected = round(
        selected.relevance_score * 0.70
        + selected.priority_score * 0.20
        + selected.recency_score * 0.10,
        6,
    )
    assert selected.final_score == expected


@pytest.mark.asyncio
async def test_inactive_debug_rows_never_consume_active_candidate_quota(
    tmp_path: Path,
) -> None:
    _, repository = await _repository(tmp_path)
    active = await repository.create(
        MemoryCreate(content="특수키워드 실제 정답", priority=0)
    )
    for index in range(35):
        await repository.create(
            MemoryCreate(
                content=f"특수키워드 보관 기록 {index}",
                active=False,
                priority=100,
            )
        )

    result = await MemoryRetriever(
        repository,
        MemoryRetrievalSettings(
            top_k=1,
            candidate_limit=30,
            minimum_relevance=0,
        ),
    ).retrieve("특수키워드", include_debug_metadata=True)

    assert [item.memory_id for item in result.selected] == [active.id]
    assert any(item.reason == "inactive" for item in result.rejected)
    assert result.candidate_count <= 40


@pytest.mark.asyncio
async def test_project_fact_queries_select_the_expected_memory(tmp_path: Path) -> None:
    """Cover the concrete Korean Phase 2.1 recall acceptance questions."""

    _, repository = await _repository(tmp_path)
    facts = {
        "nickname": await repository.create(
            MemoryCreate(content="사용자의 기본 호칭은 히냥이이다.", priority=100)
        ),
        "server": await repository.create(
            MemoryCreate(
                content="서버 PC의 시스템 RAM은 16GB이고 GPU 예약 메모리는 8GB이다.",
                category="project",
                priority=50,
            )
        ),
        "client": await repository.create(
            MemoryCreate(
                content=(
                    "클라이언트 PC 사양은 Windows 11 Pro, Ryzen 7 5700X, "
                    "RAM 32GB, RTX 3060 12GB이다."
                ),
                category="project",
                priority=50,
            )
        ),
        "model": await repository.create(
            MemoryCreate(
                content=(
                    "현재 기본 모델은 Qwen3.5-9B Q4_K_M이며 "
                    "설정된 fallback 모델은 없다."
                ),
                category="project",
                priority=50,
            )
        ),
        "architecture": await repository.create(
            MemoryCreate(
                content=(
                    "Nivelle 프로젝트에서 2PC는 두 대의 물리적 PC가 Core와 "
                    "클라이언트 역할을 나누는 구조이다."
                ),
                category="project",
                priority=50,
            )
        ),
    }
    retriever = MemoryRetriever(
        repository,
        MemoryRetrievalSettings(top_k=1, candidate_limit=10),
    )

    cases = {
        "내 호칭은 뭐야?": "nickname",
        "서버 RAM과 GPU 예약 메모리는 각각 얼마야?": "server",
        "클라이언트 PC 사양을 알려줘": "client",
        "현재 Qwen 모델과 fallback 설정은?": "model",
        "Nivelle의 2PC 구조는 무슨 뜻이야?": "architecture",
    }
    for query, expected_key in cases.items():
        result = await retriever.retrieve(query)
        assert result.selected, query
        assert result.selected[0].memory_id == facts[expected_key].id, query


@pytest.mark.asyncio
async def test_server_and_client_entity_scope_excludes_opposite_hardware(
    tmp_path: Path,
) -> None:
    _, repository = await _repository(tmp_path)
    server = await repository.create(
        MemoryCreate(
            content="서버 PC의 시스템 RAM은 16GB이고 GPU 예약 메모리는 8GB이다.",
            category="project",
        )
    )
    client = await repository.create(
        MemoryCreate(
            content="클라이언트 PC는 RAM 32GB와 RTX 3060 GPU를 사용한다.",
            category="project",
        )
    )
    retriever = MemoryRetriever(
        repository,
        MemoryRetrievalSettings(top_k=5, candidate_limit=10),
    )

    server_result = await retriever.retrieve("서버 PC RAM과 GPU 메모리는 얼마야?")
    assert server.id in {item.memory_id for item in server_result.selected}
    assert client.id not in {item.memory_id for item in server_result.selected}
    assert next(
        item for item in server_result.rejected if item.memory_id == client.id
    ).reason == "low_relevance"

    client_result = await retriever.retrieve("클라이언트 PC RAM과 GPU 사양은?")
    assert client.id in {item.memory_id for item in client_result.selected}
    assert server.id not in {item.memory_id for item in client_result.selected}
    assert next(
        item for item in client_result.rejected if item.memory_id == server.id
    ).reason == "low_relevance"


@pytest.mark.asyncio
async def test_project_name_does_not_select_every_nivelle_memory(tmp_path: Path) -> None:
    _, repository = await _repository(tmp_path)
    accent = await repository.create(
        MemoryCreate(content="Nivelle 테스트 강조색은 회색이다.", category="project")
    )
    unrelated = await repository.create(
        MemoryCreate(
            content="Nivelle 외부 접속은 사설 VPN을 우선한다.",
            category="project",
            priority=100,
        )
    )
    result = await MemoryRetriever(
        repository,
        MemoryRetrievalSettings(top_k=5, candidate_limit=10),
    ).retrieve("Nivelle 테스트 강조색은 뭐야?")

    assert [item.memory_id for item in result.selected] == [accent.id]
    assert next(
        item for item in result.rejected if item.memory_id == unrelated.id
    ).reason == "low_relevance"

@pytest.mark.asyncio
async def test_current_query_is_not_displaced_by_long_recent_context(tmp_path: Path) -> None:
    _, repository = await _repository(tmp_path)
    nickname = await repository.create(
        MemoryCreate(content="사용자의 기본 호칭은 히냥이이다.", priority=50)
    )
    await repository.create(
        MemoryCreate(content="서버 프로젝트 배포 네트워크 테스트 기록이다.", priority=100)
    )
    retriever = MemoryRetriever(
        repository,
        MemoryRetrievalSettings(top_k=1, candidate_limit=10),
    )
    long_recent = " ".join(f"이전주제{index}" for index in range(30))

    result = await retriever.retrieve(
        "내 호칭은 히냥이야?",
        recent_messages=(long_recent,),
    )

    assert result.selected
    assert result.selected[0].memory_id == nickname.id


@pytest.mark.asyncio
async def test_simple_conflicting_fact_resolution_is_deterministic(tmp_path: Path) -> None:
    database, repository = await _repository(tmp_path)
    older = await repository.create(
        MemoryCreate(content="Nivelle 테스트 강조색은 보라색이다.", priority=100)
    )
    newer = await repository.create(
        MemoryCreate(content="Nivelle 테스트 강조색은 회색이다.", priority=10)
    )
    await database.execute(
        "UPDATE memories SET updated_at=? WHERE id=?",
        ("2026-08-01T00:00:00+00:00", older.id),
    )
    await database.execute(
        "UPDATE memories SET updated_at=? WHERE id=?",
        ("2026-08-02T00:00:00+00:00", newer.id),
    )
    retriever = MemoryRetriever(
        repository,
        MemoryRetrievalSettings(top_k=5, candidate_limit=10),
    )

    automatic = await retriever.retrieve("Nivelle 테스트 강조색은 뭐야?")
    assert newer.id in {item.memory_id for item in automatic.selected}
    assert older.id not in {item.memory_id for item in automatic.selected}
    assert next(
        item for item in automatic.rejected if item.memory_id == older.id
    ).reason == "conflict_lost"

    explicitly_attached = await retriever.retrieve(
        "Nivelle 테스트 강조색은 뭐야?",
        explicitly_attached_memory_ids=(older.id,),
    )
    assert older.id in {item.memory_id for item in explicitly_attached.selected}
    assert next(
        item for item in explicitly_attached.rejected if item.memory_id == newer.id
    ).reason == "conflict_lost"


@pytest.mark.asyncio
async def test_compatible_attributes_of_one_entity_are_not_merged_as_conflicts(
    tmp_path: Path,
) -> None:
    _, repository = await _repository(tmp_path)
    occupation = await repository.create(MemoryCreate(content="사용자는 개발자이다."))
    description = await repository.create(MemoryCreate(content="사용자는 성인이다."))
    result = await MemoryRetriever(
        repository,
        MemoryRetrievalSettings(top_k=5, candidate_limit=10),
    ).retrieve("사용자는 어떤 사람이야?")

    selected_ids = {item.memory_id for item in result.selected}
    assert {occupation.id, description.id} <= selected_ids
    assert not any(item.reason == "conflict_lost" for item in result.rejected)


@pytest.mark.asyncio
async def test_retriever_explicit_inactive_deleted_duplicate_and_top_k(tmp_path: Path) -> None:
    database, repository = await _repository(tmp_path)
    first = await repository.create(MemoryCreate(content="선호 언어는 한국어이다", priority=90))
    await repository.create(MemoryCreate(content="답변은 한국어로 작성한다", priority=80))
    inactive = await repository.create(
        MemoryCreate(content="기술 설명은 한국어로 한다", active=False, priority=70)
    )
    superseded = await repository.create(
        MemoryCreate(content="선호 언어는 한국어이다", active=False, priority=10)
    )
    await database.execute(
        "UPDATE memories SET superseded_by=?,superseded_at=datetime('now') WHERE id=?",
        (first.id, superseded.id),
    )

    result = await MemoryRetriever(
        repository, MemoryRetrievalSettings(top_k=2, candidate_limit=10)
    ).retrieve(
        "한국어 답변을 작성해줘",
        explicitly_attached_memory_ids=(inactive.id, "deleted-memory-id"),
    )
    assert inactive.id in {item.memory_id for item in result.selected}
    assert next(item for item in result.selected if item.memory_id == inactive.id).reason == (
        "explicitly_attached"
    )
    assert len(result.selected) == 2
    reasons = {item.memory_id: item.reason for item in result.rejected}
    assert reasons[superseded.id] == "superseded"
    assert reasons["deleted-memory-id"] == "deleted"
    assert reasons[first.id] == "top_k_limit"
