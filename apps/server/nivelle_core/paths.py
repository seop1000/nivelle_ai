from pathlib import Path

from nivelle_protocol.local_migration import resolve_data_root
from platformdirs import user_data_path


def server_data_dir() -> Path:
    path = resolve_data_root(
        current_environment_variables=(
            "NIVELLE_CORE_DATA_DIR",
            "NIVELLE_SERVER_DATA_DIR",
        ),
        legacy_environment_variable="NOZOMI_SERVER_DATA_DIR",
        current_default=Path(user_data_path("NivelleCore", "Nivelle")),
        legacy_default=Path(user_data_path("NozomiServer", "Nozomi")),
        component="core",
    )
    for name in ("config", "database", "logs", "backups", "runtime", "pairing"):
        (path / name).mkdir(parents=True, exist_ok=True)
    return path
