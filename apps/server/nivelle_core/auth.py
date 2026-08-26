import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from .database import Database
from .repositories import now


class PairingService:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.code: str | None = None
        self.expires_at: datetime | None = None

    async def pairing_required(self) -> bool:
        row = await self.db.fetchone(
            "SELECT COUNT(*) AS n FROM clients WHERE is_admin=1 AND revoked_at IS NULL"
        )
        return not row or int(row["n"]) == 0

    def generate_code(self) -> str:
        self.code = f"{secrets.randbelow(1_000_000):06d}"
        self.expires_at = datetime.now(UTC) + timedelta(minutes=10)
        return self.code

    def pairing_available(self) -> bool:
        if self.code is None or self.expires_at is None:
            return False
        if datetime.now(UTC) > self.expires_at:
            self.code, self.expires_at = None, None
            return False
        return True

    async def complete(self, code: str, name: str) -> tuple[str, str]:
        if self.code is None or self.expires_at is None or datetime.now(UTC) > self.expires_at:
            raise ValueError("PAIRING_CODE_EXPIRED")
        if not hmac.compare_digest(self.code, code):
            raise ValueError("PAIRING_CODE_INVALID")
        # Consume before the first await so concurrent requests cannot both
        # redeem one code. A database error requires issuing a fresh code.
        self.code, self.expires_at = None, None
        is_initial_admin = await self.pairing_required()
        token, salt, client_id = secrets.token_urlsafe(48), secrets.token_hex(16), str(uuid4())
        digest = self._hash(token, salt)
        await self.db.execute(
            "INSERT INTO clients VALUES(?,?,?,?,?,NULL,NULL,?)",
            (client_id, name, digest, salt, now(), 1 if is_initial_admin else 0),
        )
        return client_id, token

    async def verify(self, token: str) -> str | None:
        return await self._verify(token, admin_only=False)

    async def verify_admin(self, token: str) -> str | None:
        """Verify a token and require the paired client to retain admin access."""
        return await self._verify(token, admin_only=True)

    async def _verify(self, token: str, *, admin_only: bool) -> str | None:
        rows = await self.db.fetchall(
            "SELECT id,token_hash,token_salt,is_admin FROM clients WHERE revoked_at IS NULL"
        )
        for row in rows:
            if (
                (not admin_only or bool(row["is_admin"]))
                and hmac.compare_digest(self._hash(token, row["token_salt"]), row["token_hash"])
            ):
                await self.db.execute(
                    "UPDATE clients SET last_seen_at=? WHERE id=?", (now(), row["id"])
                )
                return str(row["id"])
        return None

    @staticmethod
    def _hash(token: str, salt: str) -> str:
        return hashlib.pbkdf2_hmac("sha256", token.encode(), bytes.fromhex(salt), 600_000).hex()
