import asyncio

from nivelle_core.auth import PairingService
from nivelle_core.database import Database


def test_token_hash_is_deterministic_and_not_plaintext() -> None:
    value = PairingService._hash("secret", "00" * 16)
    assert value == PairingService._hash("secret", "00" * 16)
    assert "secret" not in value


async def test_admin_verification_checks_client_role(tmp_path) -> None:
    database = Database(tmp_path / "nivelle.db")
    await database.initialize()
    pairing = PairingService(database)
    code = pairing.generate_code()
    client_id, token = await pairing.complete(code, "client")

    assert await pairing.verify_admin(token) == client_id
    await database.execute("UPDATE clients SET is_admin=0 WHERE id=?", (client_id,))
    assert await pairing.verify(token) == client_id
    assert await pairing.verify_admin(token) is None


async def test_pairing_code_can_only_be_consumed_once_under_concurrency(tmp_path) -> None:
    database = Database(tmp_path / "nivelle.db")
    await database.initialize()
    pairing = PairingService(database)
    code = pairing.generate_code()

    results = await asyncio.gather(
        pairing.complete(code, "first"),
        pairing.complete(code, "second"),
        return_exceptions=True,
    )

    assert sum(isinstance(result, tuple) for result in results) == 1
    assert sum(isinstance(result, ValueError) for result in results) == 1
    row = await database.fetchone("SELECT COUNT(*) AS count FROM clients")
    assert row is not None and row["count"] == 1
