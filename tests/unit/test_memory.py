from pathlib import Path

import pytest
from nivelle_core.database import Database
from nivelle_core.memory_repository import MemoryRepository
from nivelle_protocol.memory import MemoryCreate, MemoryUpdate
from pydantic import ValidationError


async def _database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "nivelle.db")
    await database.initialize()
    return database


@pytest.mark.asyncio
async def test_memory_migration_crud_and_prompt_filter(tmp_path: Path) -> None:
    database = await _database(tmp_path)
    versions = await database.fetchall("SELECT version FROM schema_versions ORDER BY version")
    assert [row["version"] for row in versions] == [1, 2, 3, 4, 5, 6, 7, 8]

    repository = MemoryRepository(database)
    active = await repository.create(
        MemoryCreate(content="응답은 간결한 한국어로 작성한다", category="instruction", priority=80)
    )
    inactive = await repository.create(
        MemoryCreate(content="비활성 상태의 오래된 지침", active=False, priority=100)
    )
    await database.execute(
        """
        INSERT INTO memories(
            id,content,category,active,priority,explicitly_saved,
            created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            "unapproved-candidate",
            "승인되지 않은 자동 추출 후보",
            "other",
            1,
            100,
            0,
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00",
        ),
    )

    assert [item.id for item in await repository.for_prompt(1)] == [active.id]
    assert [item.id for item in await repository.search("간결한", active=True)] == [active.id]

    updated = await repository.update(active.id, MemoryUpdate(active=False))
    assert updated is not None and not updated.active
    assert await repository.for_prompt(10) == []
    assert await repository.delete(inactive.id)
    assert await repository.get(inactive.id) is None


@pytest.mark.asyncio
async def test_like_search_escapes_wildcards(tmp_path: Path) -> None:
    database = await _database(tmp_path)
    database.fts_available = False
    repository = MemoryRepository(database)
    literal = await repository.create(MemoryCreate(content="완료 기준은 100% 충족이다"))
    await repository.create(MemoryCreate(content="완료 기준은 대략 충족이다"))

    results = await repository.search("100%")
    assert [item.id for item in results] == [literal.id]


@pytest.mark.asyncio
async def test_korean_partial_search_and_prompt_ranking(tmp_path: Path) -> None:
    database = await _database(tmp_path)
    repository = MemoryRepository(database)
    relevant = await repository.create(
        MemoryCreate(
            content="Nivelle 클라이언트(Link) PC 구성은 메모리 32GB를 사용한다",
            category="project",
            priority=20,
        )
    )
    lower_relevance = await repository.create(
        MemoryCreate(
            content="Nivelle 서버(Core) 구성은 GPU를 사용한다",
            category="project",
            priority=100,
        )
    )
    inactive = await repository.create(
        MemoryCreate(
            content="Nivelle 클라이언트(Link) 사양은 메모리 64GB다",
            category="project",
            active=False,
            priority=100,
        )
    )

    particle_results = await repository.search("클라이언트의 구성은", active=True)
    assert relevant.id in {item.id for item in particle_results}

    partial_results = await repository.search("메모", active=True)
    assert [item.id for item in partial_results] == [relevant.id]

    selected = await repository.search_for_prompt("클라이언트의 사양은 뭐야?", 2)
    assert [item.id for item in selected] == [relevant.id, lower_relevance.id]
    assert inactive.id not in {item.id for item in selected}
    assert selected[0].relevance_score > selected[1].relevance_score
    assert all(item.included for item in selected)


@pytest.mark.asyncio
async def test_prompt_ranking_uses_priority_for_equal_relevance(tmp_path: Path) -> None:
    database = await _database(tmp_path)
    repository = MemoryRepository(database)
    low_priority = await repository.create(
        MemoryCreate(content="응답은 한국어로 작성한다", priority=10)
    )
    high_priority = await repository.create(
        MemoryCreate(content="설명은 한국어로 작성한다", priority=90)
    )

    selected = await repository.search_for_prompt("한국어로 답변해줘", 2)

    assert [item.id for item in selected] == [high_priority.id, low_priority.id]
    assert selected[0].relevance_score == selected[1].relevance_score


@pytest.mark.parametrize(
    "content",
    [
        "user@example.com으로 보내기",
        "연락처는 010-1234-5678",
        "주민등록번호 900101-1234567",
        "사용자: 대화 원문",
        "첫 줄\nassistant: 둘째 줄",
        "password: do-not-store-this",
    ],
)
def test_memory_rejects_direct_identifiers_and_transcripts(content: str) -> None:
    with pytest.raises(ValidationError):
        MemoryCreate(content=content)


def test_memory_update_rejects_empty_or_null_patch() -> None:
    with pytest.raises(ValidationError):
        MemoryUpdate()
    with pytest.raises(ValidationError):
        MemoryUpdate(content=None)
