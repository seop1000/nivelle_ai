from __future__ import annotations

from collections.abc import Sequence
from uuid import uuid4

import aiosqlite
from nivelle_protocol.memory import (
    MemoryContextItem,
    MemoryCreate,
    MemoryRecord,
    MemoryRetrievalSettings,
    MemoryUpdate,
)

from .database import Database
from .memory_retriever import (
    flatten_query_terms,
    normalize_memory_content,
    score_text_match,
)
from .repositories import now

_SELECT_COLUMNS = """
id,content,category,active,priority,created_at,updated_at,
superseded_by,superseded_at
"""


class DuplicateMemoryError(ValueError):
    def __init__(self, existing_memory_id: str) -> None:
        self.existing_memory_id = existing_memory_id
        super().__init__(f"an equivalent active memory already exists: {existing_memory_id}")


class MemoryRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(self, value: MemoryCreate) -> MemoryRecord:
        memory_id, timestamp = str(uuid4()), now()
        normalized_content = normalize_memory_content(value.content)
        async with aiosqlite.connect(self.db.path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("BEGIN IMMEDIATE")
            if value.active:
                duplicate = await self._duplicate_id(db, normalized_content)
                if duplicate is not None:
                    await db.rollback()
                    raise DuplicateMemoryError(duplicate)
            try:
                await db.execute(
                    """
                    INSERT INTO memories(
                        id,content,normalized_content,category,active,priority,
                        explicitly_saved,created_at,updated_at,superseded_by,superseded_at
                    ) VALUES(?,?,?,?,?,?,1,?,?,NULL,NULL)
                    """,
                    (
                        memory_id,
                        value.content,
                        normalized_content,
                        value.category,
                        int(value.active),
                        value.priority,
                        timestamp,
                        timestamp,
                    ),
                )
                await db.commit()
            except aiosqlite.IntegrityError as exc:
                await db.rollback()
                duplicate = await self._duplicate_id(db, normalized_content)
                if duplicate is not None:
                    raise DuplicateMemoryError(duplicate) from exc
                raise
        result = await self.get(memory_id)
        if result is None:  # pragma: no cover - SQLite insert/read invariant
            raise RuntimeError("created memory could not be read")
        return result

    async def get(self, memory_id: str) -> MemoryRecord | None:
        row = await self.db.fetchone(
            f"""
            SELECT {_SELECT_COLUMNS}
            FROM memories WHERE id=? AND explicitly_saved=1
            """,
            (memory_id,),
        )
        return self._record(row) if row else None

    async def get_many(self, memory_ids: Sequence[str]) -> list[MemoryRecord]:
        identifiers = list(dict.fromkeys(memory_ids))
        if not identifiers:
            return []
        placeholders = ",".join("?" for _ in identifiers)
        rows = await self.db.fetchall(
            f"""
            SELECT {_SELECT_COLUMNS}
            FROM memories
            WHERE explicitly_saved=1 AND id IN ({placeholders})
            """,
            tuple(identifiers),
        )
        by_id = {str(row["id"]): self._record(row) for row in rows}
        return [by_id[memory_id] for memory_id in identifiers if memory_id in by_id]

    async def list_all(
        self,
        *,
        active: bool | None = None,
        category: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[MemoryRecord]:
        clauses = ["explicitly_saved=1"]
        args: list[object] = []
        if active is not None:
            clauses.append("active=?")
            args.append(int(active))
        if category is not None:
            clauses.append("category=?")
            args.append(category)
        args.extend((limit, offset))
        rows = await self.db.fetchall(
            f"""
            SELECT {_SELECT_COLUMNS}
            FROM memories WHERE {' AND '.join(clauses)}
            ORDER BY priority DESC, updated_at DESC, id DESC LIMIT ? OFFSET ?
            """,
            tuple(args),
        )
        return [self._record(row) for row in rows]

    async def search(
        self, query: str, *, active: bool | None = True, limit: int = 20
    ) -> list[MemoryRecord]:
        """Run exact, FTS-prefix and bounded normalized-substring search."""

        normalized_query = normalize_memory_content(query)[:500]
        if not normalized_query or limit <= 0:
            return []
        stages: list[tuple[int, list[MemoryRecord]]] = []
        exact = await self._search_exact(normalized_query, active=active, limit=limit)
        stages.append((4, exact))
        if self.db.trigram_available:
            try:
                stages.append(
                    (3, await self._search_trigram(query, active=active, limit=limit))
                )
            except aiosqlite.OperationalError:
                self.db.trigram_available = False
        if self.db.fts_available:
            try:
                stages.append((2, await self._search_fts(query, active=active, limit=limit)))
            except aiosqlite.OperationalError:
                # A damaged or optional FTS index must never make memory CRUD unusable.
                self.db.fts_available = False
        stages.append((1, await self._search_substring(query, active=active, limit=limit)))

        merged: dict[str, tuple[int, MemoryRecord]] = {}
        for stage_score, records in stages:
            for record in records:
                previous = merged.get(record.id)
                if previous is None or stage_score > previous[0]:
                    merged[record.id] = (stage_score, record)
        defaults = MemoryRetrievalSettings.model_validate({})
        ranked = list(merged.values())
        ranked.sort(
            key=lambda value: (
                score_text_match(query, value[1].content, defaults).relevance,
                value[0],
                value[1].priority,
                value[1].updated_at,
                value[1].id,
            ),
            reverse=True,
        )
        return [record for _, record in ranked[:limit]]

    async def retrieval_candidates(
        self, query: str, *, limit: int, include_inactive_debug: bool
    ) -> list[MemoryRecord]:
        """Return active candidates plus a separately bounded debug sample.

        Inactive and superseded rows are useful for explaining exclusions, but
        they must never consume the active retrieval quota.  Otherwise a large
        archive with high priorities can hide the only relevant active fact.
        """

        if limit <= 0:
            return []
        matches = await self.search(query, active=True, limit=limit)
        by_id = {record.id: record for record in matches}
        if len(by_id) < limit:
            rows = await self.db.fetchall(
                f"""
                SELECT {_SELECT_COLUMNS}
                FROM memories
                WHERE explicitly_saved=1 AND active=1 AND superseded_by IS NULL
                ORDER BY priority DESC,updated_at DESC,id DESC
                LIMIT ?
                """,
                (limit,),
            )
            for row in rows:
                record = self._record(row)
                by_id.setdefault(record.id, record)
                if len(by_id) >= limit:
                    break
        active_records = list(by_id.values())[:limit]
        if not include_inactive_debug:
            return active_records

        # Debug rows are deliberately additive and small. Search inactive rows
        # first, then fill with recent inactive/superseded rows so the protocol
        # can still expose representative rejection reasons for auditability.
        debug_limit = min(max(limit // 3, 1), 10)
        debug_by_id = {
            record.id: record
            for record in await self.search(query, active=False, limit=debug_limit)
        }
        if len(debug_by_id) < debug_limit:
            rows = await self.db.fetchall(
                f"""
                SELECT {_SELECT_COLUMNS}
                FROM memories
                WHERE explicitly_saved=1
                  AND (active=0 OR superseded_by IS NOT NULL)
                ORDER BY priority DESC,updated_at DESC,id DESC
                LIMIT ?
                """,
                (debug_limit,),
            )
            for row in rows:
                record = self._record(row)
                debug_by_id.setdefault(record.id, record)
                if len(debug_by_id) >= debug_limit:
                    break
        return active_records + list(debug_by_id.values())[:debug_limit]

    async def search_for_prompt(self, query: str, limit: int) -> list[MemoryContextItem]:
        """Compatibility wrapper around the dedicated Phase 2.1 retriever."""

        if limit <= 0:
            return []
        from .memory_retriever import MemoryRetriever

        settings = MemoryRetrievalSettings.model_validate(
            {"top_k": min(limit, 10), "candidate_limit": max(30, limit)}
        )
        result = await MemoryRetriever(self, settings).retrieve(
            query, include_debug_metadata=False
        )
        return list(result.selected)

    async def update(self, memory_id: str, value: MemoryUpdate) -> MemoryRecord | None:
        requested = value.model_dump(exclude_unset=True)
        async with aiosqlite.connect(self.db.path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT * FROM memories WHERE id=? AND explicitly_saved=1", (memory_id,)
            )
            current = await cursor.fetchone()
            await cursor.close()
            if current is None:
                await db.rollback()
                return None

            new_content = str(requested.get("content", current["content"]))
            normalized_content = normalize_memory_content(new_content)
            new_active = bool(requested.get("active", current["active"]))
            content_changed = new_content != str(current["content"])
            revive_superseded = bool(current["superseded_by"]) and content_changed and new_active
            resulting_superseded = None if revive_superseded else current["superseded_by"]
            if new_active and resulting_superseded is None:
                duplicate = await self._duplicate_id(
                    db, normalized_content, exclude_memory_id=memory_id
                )
                if duplicate is not None:
                    await db.rollback()
                    raise DuplicateMemoryError(duplicate)

            columns: list[str] = []
            args: list[object] = []
            for field in ("content", "category", "active", "priority"):
                if field not in requested:
                    continue
                columns.append(f"{field}=?")
                field_value = requested[field]
                args.append(int(field_value) if field == "active" else field_value)
            if content_changed:
                columns.append("normalized_content=?")
                args.append(normalized_content)
            if revive_superseded:
                columns.extend(("superseded_by=NULL", "superseded_at=NULL"))
            timestamp = now()
            columns.append("updated_at=?")
            args.extend((timestamp, memory_id))
            try:
                await db.execute(
                    f"UPDATE memories SET {', '.join(columns)} WHERE id=? AND explicitly_saved=1",
                    tuple(args),
                )
                if content_changed:
                    await db.execute(
                        """
                        INSERT INTO memory_revisions(
                            memory_id,old_content,new_content,changed_at,change_source,reason
                        ) VALUES(?,?,?,?,?,?)
                        """,
                        (
                            memory_id,
                            str(current["content"]),
                            new_content,
                            timestamp,
                            "api",
                            "content updated",
                        ),
                    )
                await db.commit()
            except aiosqlite.IntegrityError as exc:
                await db.rollback()
                duplicate = await self._duplicate_id(
                    db, normalized_content, exclude_memory_id=memory_id
                )
                if duplicate is not None:
                    raise DuplicateMemoryError(duplicate) from exc
                raise
        return await self.get(memory_id)

    async def delete(self, memory_id: str) -> bool:
        async with aiosqlite.connect(self.db.path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            cursor = await db.execute(
                "DELETE FROM memories WHERE id=? AND explicitly_saved=1", (memory_id,)
            )
            deleted = cursor.rowcount > 0
            await cursor.close()
            await db.commit()
        return deleted

    async def for_prompt(self, limit: int) -> list[MemoryRecord]:
        """Legacy deterministic list; new chat code must use ``MemoryRetriever``."""

        if limit <= 0:
            return []
        rows = await self.db.fetchall(
            f"""
            SELECT {_SELECT_COLUMNS}
            FROM memories
            WHERE explicitly_saved=1 AND active=1 AND superseded_by IS NULL
            ORDER BY priority DESC,updated_at DESC,id DESC LIMIT ?
            """,
            (min(limit, 10),),
        )
        return [self._record(row) for row in rows]

    async def _search_trigram(
        self, query: str, *, active: bool | None, limit: int
    ) -> list[MemoryRecord]:
        terms = [term for term in flatten_query_terms(query) if len(term) >= 3][:20]
        if not terms:
            return []
        expression = " OR ".join(f'"{term[:64]}"' for term in terms)
        clauses = ["m.explicitly_saved=1"]
        args: list[object] = [expression]
        if active is not None:
            clauses.append("m.active=?")
            args.append(int(active))
        if active is True:
            clauses.append("m.superseded_by IS NULL")
        args.append(limit)
        rows = await self.db.fetchall(
            f"""
            SELECT m.id,m.content,m.category,m.active,m.priority,m.created_at,m.updated_at,
                   m.superseded_by,m.superseded_at
            FROM memories_trigram
            JOIN memories AS m ON m.rowid=memories_trigram.rowid
            WHERE memories_trigram MATCH ? AND {' AND '.join(clauses)}
            ORDER BY bm25(memories_trigram),m.priority DESC,m.updated_at DESC,m.id DESC
            LIMIT ?
            """,
            tuple(args),
        )
        return [self._record(row) for row in rows]

    async def _search_exact(
        self, normalized_query: str, *, active: bool | None, limit: int
    ) -> list[MemoryRecord]:
        clauses, args = self._search_state(active)
        clauses.append("(normalized_content=? OR instr(normalized_content,?)>0)")
        args.extend((normalized_query, normalized_query, limit))
        rows = await self.db.fetchall(
            f"""
            SELECT {_SELECT_COLUMNS}
            FROM memories
            WHERE {' AND '.join(clauses)}
            ORDER BY (normalized_content=?) DESC,priority DESC,updated_at DESC,id DESC
            LIMIT ?
            """,
            tuple(args[:-1] + [normalized_query, args[-1]]),
        )
        return [self._record(row) for row in rows]

    async def _search_fts(
        self, query: str, *, active: bool | None, limit: int
    ) -> list[MemoryRecord]:
        terms = flatten_query_terms(query)[:24]
        if not terms:
            return []
        expression = " OR ".join(f'"{term[:64]}"*' for term in terms)
        clauses = ["m.explicitly_saved=1"]
        args: list[object] = [expression]
        if active is not None:
            clauses.append("m.active=?")
            args.append(int(active))
        if active is True:
            clauses.append("m.superseded_by IS NULL")
        args.append(limit)
        rows = await self.db.fetchall(
            f"""
            SELECT m.id,m.content,m.category,m.active,m.priority,m.created_at,m.updated_at,
                   m.superseded_by,m.superseded_at
            FROM memories_fts
            JOIN memories AS m ON m.rowid=memories_fts.rowid
            WHERE memories_fts MATCH ? AND {' AND '.join(clauses)}
            ORDER BY bm25(memories_fts),m.priority DESC,m.updated_at DESC,m.id DESC
            LIMIT ?
            """,
            tuple(args),
        )
        return [self._record(row) for row in rows]

    async def _search_substring(
        self, query: str, *, active: bool | None, limit: int
    ) -> list[MemoryRecord]:
        terms = [term for term in flatten_query_terms(query) if len(term) >= 2][:20]
        if not terms:
            return []
        clauses, args = self._search_state(active)
        term_clauses: list[str] = []
        for term in terms:
            escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            term_clauses.append("normalized_content LIKE ? ESCAPE '\\'")
            args.append(f"%{escaped}%")
        clauses.append(f"({' OR '.join(term_clauses)})")
        args.append(limit)
        rows = await self.db.fetchall(
            f"""
            SELECT {_SELECT_COLUMNS}
            FROM memories
            WHERE {' AND '.join(clauses)}
            ORDER BY priority DESC,updated_at DESC,id DESC LIMIT ?
            """,
            tuple(args),
        )
        return [self._record(row) for row in rows]

    @staticmethod
    def _search_state(active: bool | None) -> tuple[list[str], list[object]]:
        clauses = ["explicitly_saved=1"]
        args: list[object] = []
        if active is not None:
            clauses.append("active=?")
            args.append(int(active))
        if active is True:
            clauses.append("superseded_by IS NULL")
        return clauses, args

    @staticmethod
    async def _duplicate_id(
        db: aiosqlite.Connection,
        normalized_content: str,
        *,
        exclude_memory_id: str | None = None,
    ) -> str | None:
        clauses = [
            "normalized_content=?",
            "explicitly_saved=1",
            "active=1",
            "superseded_by IS NULL",
        ]
        args: list[object] = [normalized_content]
        if exclude_memory_id is not None:
            clauses.append("id<>?")
            args.append(exclude_memory_id)
        cursor = await db.execute(
            f"SELECT id FROM memories WHERE {' AND '.join(clauses)} LIMIT 1", tuple(args)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return str(row[0]) if row else None

    @staticmethod
    def _record(row: aiosqlite.Row) -> MemoryRecord:
        return MemoryRecord.model_validate(dict(row))
