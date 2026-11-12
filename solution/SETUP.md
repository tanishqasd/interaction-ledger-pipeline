# Local setup

Run from the repository root in PowerShell with Python 3.11 or newer. The
Assignment A dependency is listed separately from the starter requirements.

```powershell
python -m venv .venv
$ledgerPython = '.\.venv\Scripts\python.exe'
& $ledgerPython -m pip install -r solution/requirements.txt
& $ledgerPython -m unittest discover -s solution/tests -v
```

To check lead ingestion with the official generator, use a fresh database:

```powershell
# Generate a secret once for a new local database. Keep it in a password
# manager if you need to open that database in another session.
$env:LEDGER_MASTER_KEY = (& $ledgerPython -c "import base64, secrets; print(base64.b64encode(secrets.token_bytes(32)).decode('ascii'))")
& $ledgerPython assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data/lead-check
& $ledgerPython -m solution init --db data/lead-check/ledger.sqlite3
& $ledgerPython -m solution ingest-leads data/lead-check/drop_1 data/lead-check/drop_2 --db data/lead-check/ledger.sqlite3
$before = (Get-FileHash -LiteralPath 'data/lead-check/ledger.sqlite3').Hash
& $ledgerPython -m solution ingest-leads data/lead-check/drop_1 data/lead-check/drop_2 --db data/lead-check/ledger.sqlite3
if ($before -ne (Get-FileHash -LiteralPath 'data/lead-check/ledger.sqlite3').Hash) { throw 'Replay changed the database.' }
```

`init` loads the workflow schemas and disposition catalogue for a new database.
On reopen, it verifies the schema version and key. `ingest-leads` reads only
`leads/<workflow>/*.csv`; this checkpoint does not import events. Use a fresh
database for the full pipeline when event ingestion is added.

Each drop is one transaction. Its directory name identifies the delivery and
its CSV paths and bytes determine its fingerprint. An identical replay is a
no-op; a changed delivery using the same name is rejected. If multiple columns
match one field, the leftmost wins. Amounts use half-up rounding and accept
properly grouped Western or Indian thousands separators. Defaults are applied
after merging incoming non-null values.

Sensitive lead fields are encrypted with AES-GCM. The indexed phone lookup uses
a separate workflow-scoped HMAC. Only the last four digits are kept for masking.
Use the same `LEDGER_MASTER_KEY` when reopening a database. Keep it out of Git;
the starter ignore rules already cover `.venv`, `data`, `.sqlite3` files and
their sidecar files.
