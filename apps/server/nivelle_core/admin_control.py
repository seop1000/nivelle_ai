"""Local-only Core identity and authentication administration.

This module deliberately exposes no HTTP route.  The desktop Core UI invokes it
inside the Gateway event loop, so pairing codes and token administration never
gain a remotely callable bypass around the normal administrator dependency.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import aiosqlite

from .agent_gateway import AgentGateway
from .auth import PairingService
from .database import Database
from .repositories import now


class CoreAdminError(RuntimeError):
    """A safe, user-facing local administration failure."""


class CoreAdminControl:
    """Perform local identity and access operations with last-admin safeguards."""

    def __init__(
        self,
        db: Database,
        pairing: PairingService,
        agent_gateway: AgentGateway,
        *,
        server_id: Callable[[], str | None],
        network_status: Callable[[], dict[str, object] | None],
        disconnect_client: Callable[[str], Awaitable[None]],
    ) -> None:
        self.db = db
        self.pairing = pairing
        self.agent_gateway = agent_gateway
        self._server_id = server_id
        self._network_status = network_status
        self._disconnect_client = disconnect_client

    async def snapshot(self) -> dict[str, object]:
        """Return the bounded local UI view without token hashes or salts."""

        rows = await self.db.fetchall(
            """
            SELECT id,name,created_at,last_seen_at,revoked_at,is_admin
            FROM clients
            ORDER BY revoked_at IS NOT NULL,is_admin DESC,created_at,id
            """
        )
        agent = await self.agent_gateway.snapshot()
        online_client_ids = {session.client_id for session in agent.sessions}
        return {
            "server_id": self._server_id(),
            "network": self._network_status(),
            "pairing": {
                "required": await self.pairing.pairing_required(),
                "available": self.pairing.pairing_available(),
                "code": self.pairing.code if self.pairing.pairing_available() else None,
                "expires_at": (
                    self.pairing.expires_at.isoformat()
                    if self.pairing.expires_at is not None
                    else None
                ),
            },
            "clients": [
                {
                    "id": str(row["id"]),
                    "name": str(row["name"]),
                    "created_at": str(row["created_at"]),
                    "last_seen_at": (
                        str(row["last_seen_at"])
                        if row["last_seen_at"] is not None
                        else None
                    ),
                    "revoked_at": (
                        str(row["revoked_at"])
                        if row["revoked_at"] is not None
                        else None
                    ),
                    "is_admin": bool(row["is_admin"]),
                    "online": str(row["id"]) in online_client_ids,
                }
                for row in rows
            ],
        }

    async def issue_pairing_code(self) -> dict[str, object]:
        code = self.pairing.generate_code()
        return {
            "code": code,
            "expires_at": (
                self.pairing.expires_at.isoformat()
                if self.pairing.expires_at is not None
                else None
            ),
        }

    async def set_admin(self, client_id: str, *, enabled: bool) -> None:
        """Change an active client's role without ever removing the last admin."""

        async with aiosqlite.connect(self.db.path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await connection.execute(
                    "SELECT id,is_admin,revoked_at FROM clients WHERE id=?",
                    (client_id,),
                )
                target = await cursor.fetchone()
                await cursor.close()
                if target is None:
                    raise CoreAdminError("선택한 클라이언트를 찾을 수 없습니다.")
                if target["revoked_at"] is not None:
                    raise CoreAdminError("인증 해제된 클라이언트의 권한은 변경할 수 없습니다.")
                currently_admin = bool(target["is_admin"])
                if currently_admin == enabled:
                    await connection.commit()
                    return
                if currently_admin and not enabled:
                    await self._require_another_admin(connection, client_id)
                await connection.execute(
                    "UPDATE clients SET is_admin=? WHERE id=?",
                    (1 if enabled else 0, client_id),
                )
                await connection.commit()
            except Exception:
                await connection.rollback()
                raise

    async def revoke_client(self, client_id: str) -> None:
        """Permanently revoke a token record and terminate its live sessions."""

        async with aiosqlite.connect(self.db.path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await connection.execute(
                    "SELECT id,is_admin,revoked_at FROM clients WHERE id=?",
                    (client_id,),
                )
                target = await cursor.fetchone()
                await cursor.close()
                if target is None:
                    raise CoreAdminError("선택한 클라이언트를 찾을 수 없습니다.")
                if target["revoked_at"] is not None:
                    await connection.commit()
                    return
                if bool(target["is_admin"]):
                    await self._require_another_admin(connection, client_id)
                await connection.execute(
                    "UPDATE clients SET revoked_at=? WHERE id=?",
                    (now(), client_id),
                )
                await connection.commit()
            except Exception:
                await connection.rollback()
                raise
        await self._disconnect_client(client_id)

    @staticmethod
    async def _require_another_admin(
        connection: aiosqlite.Connection, client_id: str
    ) -> None:
        cursor = await connection.execute(
            """
            SELECT COUNT(*)
            FROM clients
            WHERE is_admin=1 AND revoked_at IS NULL AND id<>?
            """,
            (client_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None or int(row[0]) < 1:
            raise CoreAdminError(
                "마지막 관리자의 권한 또는 인증은 해제할 수 없습니다. "
                "먼저 다른 클라이언트에 관리자 권한을 부여하세요."
            )
