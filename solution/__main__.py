"""Command-line entry point: python -m solution."""

import argparse
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from cryptography.exceptions import InvalidTag

from .db import DatabaseError, connect, initialize
from .ingest import ingest_lead_drop
from .keys import KeyConfigurationError, load_master_key
from .leads import LeadInputError
from .privacy import Privacy


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Interaction ledger and reporting pipeline")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Initialize or verify a SQLite ledger")
    ingest = commands.add_parser("ingest-leads", help="Import the lead CSVs from one or more drops")
    ingest.add_argument("drops", nargs="+", type=Path, help="Drop directories in ingest order")
    for command in (init, ingest):
        command.add_argument("--db", type=Path, default=Path("ledger.sqlite3"), help="SQLite database path")
    args = parser.parse_args(argv)

    try:
        master_key = load_master_key()
        created = initialize(args.db, master_key)
        if args.command == "init":
            action = "Initialized" if created else "Verified existing"
            print(f"{action} ledger: {args.db}")
        else:
            privacy = Privacy(master_key)
            with closing(connect(args.db)) as connection:
                for drop in args.drops:
                    stats = ingest_lead_drop(connection, privacy, drop)
                    if stats is None:
                        print(f"{drop.name}: already imported; no changes.")
                    else:
                        print(f"{drop.name}: {stats.total} lead rows, {stats.rejected} rejected, "
                              f"{stats.merged} merged, {stats.warnings} field warnings.")
    except (KeyConfigurationError, DatabaseError, LeadInputError, OSError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except InvalidTag:
        print("error: Cannot authenticate a stored lead record.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
