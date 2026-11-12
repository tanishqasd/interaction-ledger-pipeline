"""Import a named drop exactly once, in one database transaction."""

import hashlib
import json
import sqlite3
from pathlib import Path

from .leads import LeadInputError, LeadStats, ingest_batch
from .privacy import Privacy


def ingest_lead_drop(connection: sqlite3.Connection, privacy: Privacy, drop: Path) -> LeadStats | None:
    drop = drop.resolve()
    leads = drop / "leads"
    if not leads.is_dir():
        raise LeadInputError(f"Drop {drop.name} does not contain a leads directory.")

    # Hash and parse the same bytes. Retrying a delivery checks its contents, not its path or mtime.
    paths = sorted(leads.glob("*/*.csv"), key=lambda path: (path.name, path.parent.name))
    files = [(path, path.read_bytes()) for path in paths]
    manifest = [(path.relative_to(drop).as_posix(), hashlib.sha256(content).hexdigest())
                for path, content in files]
    digest = privacy.manifest_digest(json.dumps(manifest, separators=(",", ":")).encode("utf-8"))

    with connection:
        connection.execute("BEGIN IMMEDIATE")
        previous = connection.execute(
            "SELECT manifest_digest FROM ingested_drops WHERE source_key = ?", (drop.name,),
        ).fetchone()
        if previous is not None:
            if previous["manifest_digest"] != digest:
                raise LeadInputError(f"Drop {drop.name} was already imported with different lead files.")
            return None

        drop_id = connection.execute(
            "INSERT INTO ingested_drops (source_key, manifest_digest) VALUES (?, ?)",
            (drop.name, digest),
        ).lastrowid
        schemas = {
            row["workflow_id"]: json.loads(row["schema_json"])["fields"]
            for row in connection.execute("SELECT workflow_id, schema_json FROM workflows")
        }
        stats = LeadStats()
        for path, content in files:
            workflow_id = path.parent.name
            if workflow_id not in schemas:
                raise LeadInputError(f"Unknown workflow in drop {drop.name}: {workflow_id}.")
            ingest_batch(connection, privacy, drop_id, workflow_id, path.stem,
                         schemas[workflow_id], content, stats)
        connection.execute(
            """UPDATE ingested_drops SET lead_rows_total = ?, duplicate_lead_rows_merged = ?,
               field_warnings = ? WHERE drop_id = ?""", (stats.total, stats.merged, stats.warnings, drop_id),
        )
    return stats
