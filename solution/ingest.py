"""Import a named drop exactly once, in one database transaction."""

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .events import EventStats, ingest_event_file
from .leads import LeadInputError, LeadStats, ingest_batch
from .privacy import Privacy


@dataclass
class DropStats:
    leads: LeadStats = field(default_factory=LeadStats)
    events: EventStats = field(default_factory=EventStats)


def _ingest_drop(
    connection: sqlite3.Connection, privacy: Privacy, drop: Path, *, include_events: bool,
) -> DropStats | None:
    drop = drop.resolve()
    leads = drop / "leads"
    events = drop / "events"
    if not leads.is_dir() and not (include_events and events.is_dir()):
        expected = "a leads or events directory" if include_events else "a leads directory"
        raise LeadInputError(f"Drop {drop.name} does not contain {expected}.")

    # Hash and parse the same bytes. Retrying a delivery checks its contents, not its path or mtime.
    lead_paths = sorted(leads.glob("*/*.csv"), key=lambda path: (path.name, path.parent.name))
    event_paths = sorted(events.glob("*.jsonl")) if include_events else []
    lead_files = [(path, path.read_bytes()) for path in lead_paths]
    event_files = [(path, path.read_bytes()) for path in event_paths]
    manifest = [(path.relative_to(drop).as_posix(), hashlib.sha256(content).hexdigest())
                for path, content in (*lead_files, *event_files)]
    digest = privacy.manifest_digest(json.dumps(manifest, separators=(",", ":")).encode("utf-8"))

    with connection:
        connection.execute("BEGIN IMMEDIATE")
        previous = connection.execute(
            "SELECT manifest_digest FROM ingested_drops WHERE source_key = ?", (drop.name,),
        ).fetchone()
        if previous is not None:
            if previous["manifest_digest"] != digest:
                description = "input files" if include_events else "lead files"
                raise LeadInputError(f"Drop {drop.name} was already imported with different {description}.")
            return None

        drop_id = connection.execute(
            "INSERT INTO ingested_drops (source_key, manifest_digest) VALUES (?, ?)",
            (drop.name, digest),
        ).lastrowid
        schemas = {
            row["workflow_id"]: json.loads(row["schema_json"])["fields"]
            for row in connection.execute("SELECT workflow_id, schema_json FROM workflows")
        }
        stats = DropStats()
        for path, content in lead_files:
            workflow_id = path.parent.name
            if workflow_id not in schemas:
                raise LeadInputError(f"Unknown workflow in drop {drop.name}: {workflow_id}.")
            ingest_batch(connection, privacy, drop_id, workflow_id, path.stem,
                         schemas[workflow_id], content, stats.leads)

        if event_files:
            catalogue = {tuple(row) for row in connection.execute("SELECT workflow_id, code FROM dispositions")}
            for path, content in event_files:
                ingest_event_file(connection, privacy, drop_id, path.name, content, catalogue, stats.events)

        connection.execute(
            """UPDATE ingested_drops SET lead_rows_total = ?, duplicate_lead_rows_merged = ?,
               field_warnings = ?, events_total = ?, malformed_events = ?, duplicate_events_dropped = ?
               WHERE drop_id = ?""",
            (stats.leads.total, stats.leads.merged, stats.leads.warnings,
             stats.events.total, stats.events.malformed, stats.events.duplicates, drop_id),
        )
    return stats


def ingest_lead_drop(connection: sqlite3.Connection, privacy: Privacy, drop: Path) -> LeadStats | None:
    stats = _ingest_drop(connection, privacy, drop, include_events=False)
    return None if stats is None else stats.leads


def ingest_drop(connection: sqlite3.Connection, privacy: Privacy, drop: Path) -> DropStats | None:
    return _ingest_drop(connection, privacy, drop, include_events=True)
