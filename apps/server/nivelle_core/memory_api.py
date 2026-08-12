from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from nivelle_protocol.memory import MemoryCategory, MemoryCreate, MemoryRecord, MemoryUpdate

from .memory_repository import DuplicateMemoryError, MemoryRepository


def create_memory_router(
    repository: MemoryRepository,
    auth_dependency: Callable[..., Awaitable[str]],
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/memories", tags=["memories"])

    @router.post("", response_model=MemoryRecord, status_code=status.HTTP_201_CREATED)
    async def create_memory(
        body: MemoryCreate, _: str = Depends(auth_dependency)
    ) -> MemoryRecord:
        try:
            return await repository.create(body)
        except DuplicateMemoryError as exc:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                {
                    "code": "MEMORY_DUPLICATE",
                    "message": "동일한 활성 기억이 이미 있습니다.",
                    "existing_memory_id": exc.existing_memory_id,
                },
            ) from exc

    @router.get("", response_model=list[MemoryRecord])
    async def list_memories(
        active: bool | None = None,
        category: MemoryCategory | None = None,
        limit: Annotated[int, Query(ge=1, le=50)] = 20,
        offset: Annotated[int, Query(ge=0)] = 0,
        _: str = Depends(auth_dependency),
    ) -> list[MemoryRecord]:
        return await repository.list_all(
            active=active, category=category, limit=limit, offset=offset
        )

    @router.get("/search", response_model=list[MemoryRecord])
    async def search_memories(
        q: Annotated[str, Query(min_length=1, max_length=200)],
        active: bool | None = True,
        include_inactive: bool = False,
        limit: Annotated[int, Query(ge=1, le=50)] = 20,
        _: str = Depends(auth_dependency),
    ) -> list[MemoryRecord]:
        return await repository.search(
            q, active=None if include_inactive else active, limit=limit
        )

    @router.get("/{memory_id}", response_model=MemoryRecord)
    async def get_memory(
        memory_id: str, _: str = Depends(auth_dependency)
    ) -> MemoryRecord:
        memory = await repository.get(memory_id)
        if memory is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "memory not found")
        return memory

    @router.patch("/{memory_id}", response_model=MemoryRecord)
    async def update_memory(
        memory_id: str,
        body: MemoryUpdate,
        _: str = Depends(auth_dependency),
    ) -> MemoryRecord:
        try:
            memory = await repository.update(memory_id, body)
        except DuplicateMemoryError as exc:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                {
                    "code": "MEMORY_DUPLICATE",
                    "message": "동일한 활성 기억이 이미 있습니다.",
                    "existing_memory_id": exc.existing_memory_id,
                },
            ) from exc
        if memory is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "memory not found")
        return memory

    @router.delete("/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_memory(memory_id: str, _: str = Depends(auth_dependency)) -> Response:
        if not await repository.delete(memory_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "memory not found")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router
