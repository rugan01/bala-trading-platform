from __future__ import annotations

import sqlite3
from pathlib import Path

from trading_platform.archive.schema import PRAGMAS, SCHEMA_STATEMENTS
from trading_platform.paths import ARCHIVE_ROOT, REPO_ROOT

PROJECT_ROOT = REPO_ROOT
DEFAULT_DB_PATH = ARCHIVE_ROOT / "platform.sqlite3"


def initialize_database(db_path: Path = DEFAULT_DB_PATH) -> Path:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(db_path) as conn:
        for pragma in PRAGMAS:
            conn.execute(pragma)
        for statement in SCHEMA_STATEMENTS:
            conn.execute(statement)
        conn.commit()

    return db_path
