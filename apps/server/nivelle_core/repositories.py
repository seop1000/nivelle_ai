import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import aiosqlite

from .database import Database

HISTORY_MESSAGE_LIMIT = 20


def now() -> str:
    return datetime.now(UTC).isoformat()


class ConversationRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(self, first_message: str) -> dict[str, str]:
        cid, timestamp = str(uuid4()), now()
        title = " ".join(first_message.split())[:60] or "새 대화"
        await self.db.execute(
            "INSERT INTO conversations VALUES(?,?,?,?,NULL)", (cid, title, timestamp, timestamp)
        )
        return {"id": cid, "title": title, "created_at": timestamp, "updated_at": timestamp}

    async def list_all(self) -> list[dict[str, object]]:
        rows = await self.db.fetchall(
            "SELECT * FROM conversations WHERE archived_at IS NULL ORDER BY updated_at DESC"
        )
        return [dict(row) for row in rows]

    async def completed_messages(
        self, conversation_id: str
    ) -> list[dict[str, object]] | None:
        """Return prompt-safe history for an active conversation.

        ``None`` distinguishes an unknown or archived conversation from an active
        conversation that does not have any completed messages yet.
        """

        conversation = await self.db.fetchone(
            "SELECT id FROM conversations WHERE id=? AND archived_at IS NULL",
            (conversation_id,),
        )
        if conversation is None:
            return None
        rows = await self.db.fetchall(
            """
            SELECT role,content FROM (
                SELECT id,role,content,created_at FROM messages
                WHERE conversation_id=?
                  AND state='completed'
                  AND role IN ('user','assistant')
                ORDER BY created_at DESC,id DESC
                LIMIT ?
            ) AS recent_messages
            ORDER BY created_at,id
            """,
            (conversation_id, HISTORY_MESSAGE_LIMIT),
        )
        return [dict(row) for row in rows]

    async def add_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        state: str = "completed",
        *,
        metadata: dict[str, Any] | None = None,
        message_id: str | None = None,
        client_message_id: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, object]:
        mid, timestamp = message_id or str(uuid4()), now()
        serialized_metadata = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)
        await self.db.execute(
            """
            INSERT INTO messages(
                id,conversation_id,role,content,created_at,state,
                prompt_tokens,completion_tokens,metadata_json,client_message_id,request_id
            ) VALUES(?,?,?,?,?,?,NULL,NULL,?,?,?)
            """,
            (
                mid,
                conversation_id,
                role,
                content,
                timestamp,
                state,
                serialized_metadata,
                client_message_id,
                request_id,
            ),
        )
        await self.db.execute(
            "UPDATE conversations SET updated_at=? WHERE id=?", (timestamp, conversation_id)
        )
        return {
            "id": mid,
            "conversation_id": conversation_id,
            "role": role,
            "content": content,
            "created_at": timestamp,
            "state": state,
            "metadata_json": serialized_metadata,
            "client_message_id": client_message_id,
            "request_id": request_id,
        }

    async def allocate_turn(
        self,
        conversation_id: str,
        user_content: str,
        *,
        user_metadata: dict[str, Any],
        assistant_metadata: dict[str, Any],
        client_message_id: str,
        request_id: str | None = None,
        retry_of_client_message_id: str | None = None,
    ) -> tuple[dict[str, object], dict[str, object]]:
        """Persist a user message and assistant placeholder atomically."""

        user_id = str(uuid4())
        assistant_id = str(uuid4())
        timestamp = now()
        assistant_timestamp = (
            datetime.fromisoformat(timestamp) + timedelta(microseconds=1)
        ).isoformat()
        serialized_user_metadata = json.dumps(
            user_metadata, ensure_ascii=False, sort_keys=True
        )
        serialized_assistant_metadata = json.dumps(
            assistant_metadata, ensure_ascii=False, sort_keys=True
        )
        async with aiosqlite.connect(self.db.path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute(
                    """
                    INSERT INTO messages(
                        id,conversation_id,role,content,created_at,state,
                        prompt_tokens,completion_tokens,metadata_json,client_message_id,
                        retry_of_client_message_id,request_id
                    ) VALUES(?,?,?,?,?,'completed',NULL,NULL,?,?,?,?)
                    """,
                    (
                        user_id,
                        conversation_id,
                        "user",
                        user_content,
                        timestamp,
                        serialized_user_metadata,
                        client_message_id,
                        retry_of_client_message_id,
                        request_id,
                    ),
                )
                await db.execute(
                    """
                    INSERT INTO messages(
                        id,conversation_id,role,content,created_at,state,
                        prompt_tokens,completion_tokens,metadata_json,client_message_id
                    ) VALUES(?,?,?,?,?,'generating',NULL,NULL,?,NULL)
                    """,
                    (
                        assistant_id,
                        conversation_id,
                        "assistant",
                        "",
                        assistant_timestamp,
                        serialized_assistant_metadata,
                    ),
                )
                await db.execute(
                    "UPDATE conversations SET updated_at=? WHERE id=?",
                    (assistant_timestamp, conversation_id),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return (
            {
                "id": user_id,
                "conversation_id": conversation_id,
                "role": "user",
                "content": user_content,
                "created_at": timestamp,
                "state": "completed",
                "metadata_json": serialized_user_metadata,
                "client_message_id": client_message_id,
                "retry_of_client_message_id": retry_of_client_message_id,
                "request_id": request_id,
            },
            {
                "id": assistant_id,
                "conversation_id": conversation_id,
                "role": "assistant",
                "content": "",
                "created_at": assistant_timestamp,
                "state": "generating",
                "metadata_json": serialized_assistant_metadata,
                "client_message_id": None,
            },
        )

    async def update_message(
        self,
        message_id: str,
        *,
        content: str,
        state: str,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        expected_state: str | None = None,
    ) -> dict[str, object] | None:
        """Update an allocated assistant message without changing its identity.

        ``expected_state`` makes terminal transitions monotonic: a late socket
        cancellation can never turn a durably completed response back into an
        interrupted one.
        """

        clauses = ["id=?"]
        args: list[object] = [
            content,
            state,
            prompt_tokens,
            completion_tokens,
            message_id,
        ]
        if expected_state is not None:
            clauses.append("state=?")
            args.append(expected_state)
        async with aiosqlite.connect(self.db.path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    f"""
                    UPDATE messages
                    SET content=?,state=?,prompt_tokens=?,completion_tokens=?
                    WHERE {' AND '.join(clauses)}
                    """,
                    tuple(args),
                )
                changed = cursor.rowcount > 0
                await cursor.close()
                row_cursor = await db.execute(
                    "SELECT * FROM messages WHERE id=?", (message_id,)
                )
                row = await row_cursor.fetchone()
                await row_cursor.close()
                if row is not None and changed:
                    await db.execute(
                        "UPDATE conversations SET updated_at=? WHERE id=?",
                        (now(), str(row["conversation_id"])),
                    )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return dict(row) if row is not None else None

    async def retry_target(
        self, client_message_id: str
    ) -> tuple[dict[str, object], dict[str, object] | None] | None:
        """Return a user request and the assistant allocated in reply to it."""

        user = await self.find_user_by_client_message_id(client_message_id)
        if user is None:
            return None
        rows = await self.db.fetchall(
            """
            SELECT * FROM messages
            WHERE conversation_id=? AND role='assistant'
            ORDER BY created_at,id
            """,
            (str(user["conversation_id"]),),
        )
        for row in rows:
            try:
                metadata = json.loads(str(row["metadata_json"] or "{}"))
            except (json.JSONDecodeError, TypeError):
                continue
            if metadata.get("in_reply_to_client_message_id") == client_message_id:
                return user, dict(row)
        return user, None

    async def find_retry_by_target(
        self, retry_of_client_message_id: str
    ) -> dict[str, object] | None:
        row = await self.db.fetchone(
            """
            SELECT * FROM messages
            WHERE role='user' AND retry_of_client_message_id=?
            ORDER BY created_at,id LIMIT 1
            """,
            (retry_of_client_message_id,),
        )
        return dict(row) if row is not None else None

    async def recover_interrupted_generations(self) -> dict[str, int]:
        """Recover process-local generation state left by an unclean shutdown."""

        recovered_at = now()
        assistant_count = 0
        orphan_user_count = 0
        async with aiosqlite.connect(self.db.path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                assistant_cursor = await db.execute(
                    """
                    SELECT id,metadata_json FROM messages
                    WHERE role='assistant' AND state='generating'
                    """
                )
                generating = await assistant_cursor.fetchall()
                await assistant_cursor.close()
                for row in generating:
                    metadata = _metadata_dict(row["metadata_json"])
                    metadata.update(
                        {
                            "recovery_reason": "server_restart",
                            "recovered_at": recovered_at,
                        }
                    )
                    cursor = await db.execute(
                        """
                        UPDATE messages SET state='interrupted',metadata_json=?
                        WHERE id=? AND state='generating'
                        """,
                        (
                            json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                            str(row["id"]),
                        ),
                    )
                    assistant_count += max(cursor.rowcount, 0)
                    await cursor.close()

                assistant_cursor = await db.execute(
                    "SELECT metadata_json FROM messages WHERE role='assistant'"
                )
                assistant_rows = await assistant_cursor.fetchall()
                await assistant_cursor.close()
                replied_ids = {
                    str(reply_id)
                    for row in assistant_rows
                    if (
                        reply_id := _metadata_dict(row["metadata_json"]).get(
                            "in_reply_to_client_message_id"
                        )
                    )
                }
                user_cursor = await db.execute(
                    """
                    SELECT id,client_message_id,metadata_json FROM messages
                    WHERE role='user' AND state='completed'
                      AND client_message_id IS NOT NULL
                    """
                )
                users = await user_cursor.fetchall()
                await user_cursor.close()
                for row in users:
                    client_id = str(row["client_message_id"])
                    if client_id in replied_ids:
                        continue
                    metadata = _metadata_dict(row["metadata_json"])
                    metadata.update(
                        {
                            "recovery_reason": "orphaned_user_turn",
                            "recovered_at": recovered_at,
                        }
                    )
                    cursor = await db.execute(
                        """
                        UPDATE messages SET state='interrupted',metadata_json=?
                        WHERE id=? AND state='completed'
                        """,
                        (
                            json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                            str(row["id"]),
                        ),
                    )
                    orphan_user_count += max(cursor.rowcount, 0)
                    await cursor.close()
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return {
            "assistant_messages": assistant_count,
            "orphan_user_messages": orphan_user_count,
        }

    async def find_user_by_client_message_id(
        self, client_message_id: str, conversation_id: str | None = None
    ) -> dict[str, object] | None:
        """Find a persisted user turn by its stable client-generated identity."""
        if conversation_id is None:
            row = await self.db.fetchone(
                """
                SELECT * FROM messages
                WHERE role='user' AND client_message_id=?
                ORDER BY created_at DESC,id DESC LIMIT 1
                """,
                (client_message_id,),
            )
        else:
            row = await self.db.fetchone(
                """
                SELECT * FROM messages
                WHERE role='user' AND conversation_id=? AND client_message_id=?
                ORDER BY created_at DESC,id DESC LIMIT 1
                """,
                (conversation_id, client_message_id),
            )
        return dict(row) if row is not None else None

    async def find_user_by_request_id(self, request_id: str) -> dict[str, object] | None:
        """Find the durable user turn for a transport request identity."""

        row = await self.db.fetchone(
            """
            SELECT * FROM messages
            WHERE role='user' AND request_id=?
            ORDER BY created_at DESC,id DESC LIMIT 1
            """,
            (request_id,),
        )
        return dict(row) if row is not None else None

    async def messages(self, conversation_id: str) -> list[dict[str, object]]:
        rows = await self.db.fetchall(
            "SELECT * FROM messages WHERE conversation_id=? ORDER BY created_at", (conversation_id,)
        )
        return [dict(row) for row in rows]


def _metadata_dict(value: object) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
