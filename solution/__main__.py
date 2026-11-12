"""Command-line entry point: python -m solution."""

import argparse
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from cryptography.exceptions import InvalidTag

from .db import DatabaseError, connect, initialize
from .events import EventInputError
from .ingest import ingest_drop, ingest_lead_drop
from .keys import KeyConfigurationError, load_master_key
from .leads import LeadInputError
from .privacy import Privacy


def _ingest(args: argparse.Namespace, master_key: bytes) -> None:
    privacy = Privacy(master_key)
    full_drop = args.command == "ingest"
    with closing(connect(args.db)) as connection:
        for drop in args.drops:
            if full_drop:
                stats = ingest_drop(connection, privacy, drop)
                leads = None if stats is None else stats.leads
            else:
                stats = None
                leads = ingest_lead_drop(connection, privacy, drop)
            if leads is None:
                print(f"{drop.name}: already imported; no changes.")
                continue
            print(f"{drop.name}: {leads.total} lead rows, {leads.rejected} rejected, "
                  f"{leads.merged} merged, {leads.warnings} field warnings.")
            if stats is not None:
                events = stats.events
                print(f"{drop.name}: {events.total} event lines, {events.malformed} malformed, "
                      f"{events.duplicates} duplicates.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Interaction ledger and reporting pipeline")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Initialize or verify a SQLite ledger")
    ingest = commands.add_parser("ingest", help="Import lead CSVs and JSONL events from one or more drops")
    lead_only = commands.add_parser("ingest-leads", help="Import only the lead CSVs from one or more drops")
    for command in (ingest, lead_only):
        command.add_argument("drops", nargs="+", type=Path, help="Drop directories in ingest order")
    for command in (init, ingest, lead_only):
        command.add_argument("--db", type=Path, default=Path("ledger.sqlite3"), help="SQLite database path")
    args = parser.parse_args(argv)

    try:
        master_key = load_master_key()
        created = initialize(args.db, master_key)
        if args.command == "init":
            action = "Initialized" if created else "Verified existing"
            print(f"{action} ledger: {args.db}")
        else:
            _ingest(args, master_key)
    except (KeyConfigurationError, DatabaseError, LeadInputError, EventInputError, OSError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except InvalidTag:
        print("error: Cannot authenticate a stored lead record.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
