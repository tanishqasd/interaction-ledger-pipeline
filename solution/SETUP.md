# Local setup

Run the commands from the repository root in PowerShell. This milestone uses
Python 3.11+ and the standard library, including SQLite and `unittest`.

```powershell
python -m venv .venv
$ledgerPython = '.\.venv\Scripts\python.exe'
& $ledgerPython -c "import sqlite3; print(sqlite3.sqlite_version)"

# Generate a secret once for a new local database. Keep it in a password
# manager if you need to open that database in another session.
$env:LEDGER_MASTER_KEY = (& $ledgerPython -c "import base64, secrets; print(base64.b64encode(secrets.token_bytes(32)).decode('ascii'))")
& $ledgerPython -m solution init --db ledger.sqlite3
& $ledgerPython -m solution init --db ledger.sqlite3
& $ledgerPython -m unittest discover -s solution/tests -v
```

The first `init` loads the workflow schemas and disposition catalogue. The
second verifies the existing schema version and key without rewriting data.
Use the same `LEDGER_MASTER_KEY` when reopening a database; generating a new
secret is only for a new database. Keep the key out of files tracked by Git.
Use the `.sqlite3` extension so the starter ignore rules cover the database
and SQLite sidecar files. `python -m solution --help` lists available commands.
