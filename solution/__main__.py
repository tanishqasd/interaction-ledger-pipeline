"""Command-line entry point: python -m solution."""

import argparse
import sqlite3
import sys
from pathlib import Path

from .db import DatabaseError, initialize
from .keys import KeyConfigurationError, load_master_key


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Interaction ledger and reporting pipeline")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Initialize or verify a SQLite ledger")
    init.add_argument("--db", type=Path, default=Path("ledger.sqlite3"), help="SQLite database path")
    args = parser.parse_args(argv)

    try:
        created = initialize(args.db, load_master_key())
    except (KeyConfigurationError, DatabaseError, OSError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    action = "Initialized" if created else "Verified existing"
    print(f"{action} ledger: {args.db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
