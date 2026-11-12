"""Write the five public reports from one read-only SQLite snapshot."""

import csv
import json
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from .calls import InteractionSnapshot, derive_interactions, lead_bucket
from .db import DatabaseError, connect
from .schema import SCHEMA_VERSION

_BUCKETS = ("yet_to_reach_out", "in_progress_telephony_failed", "in_progress_dialed", "completed")
_HEADERS = {
    "batch_summary.csv": ("workflow_id", "batch_id", "total_leads", *_BUCKETS),
    "daily_overview.csv": ("workflow_id", "date", "total_calls", "connected_calls",
                           "connectivity_rate", "success_leads"),
    "lead_report.csv": ("workflow_id", "external_ref", "batch_id", "phone_masked", "bucket",
                        "total_calls", "connected_calls", "last_call_at", "current_disposition", "ever_success"),
    "rejections.csv": ("workflow_id", "batch_id", "row_number", "reason"),
}
_QUALITY_FILE = "data_quality.json"


@dataclass
class _DailyCounts:
    total: int = 0
    connected: int = 0
    successful_leads: set[int] = field(default_factory=set)


def _lead_and_call_reports(leads: list[sqlite3.Row], snapshot: InteractionSnapshot) -> dict[str, list[tuple]]:
    batches: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    days: dict[tuple[str, str], _DailyCounts] = defaultdict(_DailyCounts)
    lead_rows = []
    for lead in leads:
        lead_id, workflow, reference, batch, last_four = lead
        calls = snapshot.calls_by_lead.get(lead_id, [])
        bucket = lead_bucket(calls)
        batches[workflow, batch][bucket] += 1

        connected = 0
        ever_success = False
        current_disposition = ""
        last_call_at = ""
        # Calls are in initiation order. An undispositioned call leaves the previous code in place.
        for call in calls:
            initiated = call.initiated_at_ist
            last_call_at = initiated.isoformat()
            day = days[workflow, initiated.date().isoformat()]
            day.total += 1
            day.connected += call.connected
            connected += call.connected
            if call.disposition is not None:
                current_disposition = call.disposition.code
                if call.disposition.is_success:
                    day.successful_leads.add(lead_id)
                    ever_success = True

        lead_rows.append((workflow, reference, batch, f"+91******{last_four}", bucket, len(calls),
                          connected, last_call_at, current_disposition, "true" if ever_success else "false"))

    return {
        "batch_summary.csv": [
            (*key, sum(counts.values()), *(counts[bucket] for bucket in _BUCKETS))
            for key, counts in sorted(batches.items())
        ],
        "daily_overview.csv": [
            (*key, day.total, day.connected, f"{Decimal(day.connected) / day.total:.4f}", len(day.successful_leads))
            for key, day in sorted(days.items())
        ],
        "lead_report.csv": lead_rows,
    }


def _data_quality(connection: sqlite3.Connection, rejections: list[tuple], snapshot: InteractionSnapshot) -> dict:
    totals = connection.execute(
        """SELECT COALESCE(SUM(lead_rows_total), 0), COALESCE(SUM(duplicate_lead_rows_merged), 0),
                  COALESCE(SUM(field_warnings), 0), COALESCE(SUM(events_total), 0),
                  COALESCE(SUM(malformed_events), 0), COALESCE(SUM(duplicate_events_dropped), 0)
           FROM ingested_drops"""
    ).fetchone()
    lead_rows, merged, warnings, event_lines, malformed, duplicates = totals
    return {
        "lead_rows_total": lead_rows,
        "lead_rows_rejected": len(rejections),
        "rejections_by_reason": dict(sorted(Counter(row[3] for row in rejections).items())),
        "duplicate_lead_rows_merged": merged,
        "field_warnings": warnings,
        "events_total": event_lines,
        "malformed_events": malformed,
        "duplicate_events_dropped": duplicates,
        "orphan_events": snapshot.orphan_events,
        "unknown_disposition_events": snapshot.unknown_disposition_events,
    }


def write_reports(database: Path, out_dir: Path) -> None:
    with closing(connect(database, read_only=True)) as connection, connection:
        connection.execute("BEGIN")
        if connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise DatabaseError("The database is not an initialized ledger with a supported schema version.")
        snapshot = derive_interactions(connection)
        leads = connection.execute(
            """SELECT l.lead_id, l.workflow_id, l.external_ref, b.batch_id, l.phone_last4
               FROM leads l JOIN batches b ON b.batch_pk = l.first_batch_pk
               ORDER BY l.workflow_id, l.external_ref, l.lead_id"""
        ).fetchall()
        tables = _lead_and_call_reports(leads, snapshot)
        rejections = [tuple(row) for row in connection.execute(
            """SELECT b.workflow_id, b.batch_id, r.row_number, r.reason
               FROM rejections r JOIN batches b ON b.batch_pk = r.batch_pk
               ORDER BY b.workflow_id, b.batch_id, r.row_number, b.drop_id"""
        )]
        tables["rejections.csv"] = rejections
        quality = _data_quality(connection, rejections, snapshot)

    names = (*_HEADERS, _QUALITY_FILE)
    if any((out_dir / name).resolve() == database.resolve() for name in names):
        raise DatabaseError("A report destination would overwrite the ledger database.")
    out_dir.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=out_dir, prefix=".report-") as temp:
        staging = Path(temp)
        for name, header in _HEADERS.items():
            with (staging / name).open("w", encoding="utf-8", newline="") as file:
                writer = csv.writer(file, lineterminator="\r\n")
                writer.writerow(header)
                writer.writerows(tables[name])
        with (staging / _QUALITY_FILE).open("w", encoding="utf-8", newline="\n") as file:
            json.dump(quality, file, indent=2)
            file.write("\n")
        for name in names:
            (staging / name).replace(out_dir / name)
