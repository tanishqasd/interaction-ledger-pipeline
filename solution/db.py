"""SQLite connections and initialization from the starter reference files."""

import csv
import hmac
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from .keys import KEY_ENV, key_check
from .schema import SCHEMA, SCHEMA_VERSION

REFERENCE_DIR = Path(__file__).resolve().parent.parent / "assignment-a-data" / "schemas"


class DatabaseError(RuntimeError):
    pass


def connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    # REPLACE can delete the existing row; recursive triggers also guard that path.
    connection.execute("PRAGMA recursive_triggers = ON")
    return connection


def _load_references(connection: sqlite3.Connection) -> None:
    for path in sorted(REFERENCE_DIR.glob("*.json")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        connection.execute(
            "INSERT INTO workflows (workflow_id, schema_json) VALUES (?, ?)",
            (schema["workflow_id"], json.dumps(schema, ensure_ascii=False)),
        )

    flags = {"true": 1, "false": 0}
    with (REFERENCE_DIR / "dispositions.csv").open(encoding="utf-8", newline="") as file:
        connection.executemany(
            """INSERT INTO dispositions (workflow_id, code, label, is_terminal, is_success)
               VALUES (?, ?, ?, ?, ?)""",
            (
                (row["workflow_id"], row["code"], row["label"],
                 flags[row["is_terminal"]], flags[row["is_success"]])
                for row in csv.DictReader(file)
            ),
        )


def initialize(path: Path, master_key: bytes) -> bool:
    """Create a ledger atomically, or verify the version and secret of an existing one."""
    tag = key_check(master_key)
    with closing(connect(path)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version == SCHEMA_VERSION:
            row = connection.execute("SELECT key_tag FROM pipeline_meta WHERE singleton = 1").fetchone()
            if row is None:
                raise DatabaseError("Ledger metadata is missing.")
            if not hmac.compare_digest(tag, row["key_tag"]):
                raise DatabaseError(f"{KEY_ENV} does not match this database.")
            return False
        if version != 0:
            raise DatabaseError(f"Unsupported ledger schema version: {version}.")
        if connection.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone() is not None:
            raise DatabaseError("The SQLite file is not an empty database or a supported ledger.")

        for statement in SCHEMA:
            connection.execute(statement)
        _load_references(connection)
        connection.execute("INSERT INTO pipeline_meta (singleton, key_tag) VALUES (1, ?)", (tag,))
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        return True
