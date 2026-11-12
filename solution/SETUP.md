# Local setup

Run from the repository root in PowerShell with Python 3.11 or newer. The
Assignment A dependency is listed separately from the starter requirements.

```powershell
python -m venv .venv
$ledgerPython = '.\.venv\Scripts\python.exe'
& $ledgerPython -m pip install -r solution/requirements.txt
& $ledgerPython -m unittest discover -s solution/tests -v
```

The test suite runs the official generator with seeds 42 and 2026 in temporary
directories. To try the full ingest command yourself, use a fresh database:

```powershell
# Generate a secret once for a new local database. Keep it in a password
# manager if you need to open that database in another session.
$env:LEDGER_MASTER_KEY = (& $ledgerPython -c "import base64, secrets; print(base64.b64encode(secrets.token_bytes(32)).decode('ascii'))")
& $ledgerPython assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data/event-check
& $ledgerPython -m solution init --db data/event-check/ledger.sqlite3
& $ledgerPython -m solution ingest data/event-check/drop_1 data/event-check/drop_2 --db data/event-check/ledger.sqlite3
$before = (Get-FileHash -LiteralPath 'data/event-check/ledger.sqlite3').Hash
& $ledgerPython -m solution ingest data/event-check/drop_1 data/event-check/drop_2 --db data/event-check/ledger.sqlite3
if ($before -ne (Get-FileHash -LiteralPath 'data/event-check/ledger.sqlite3').Hash) { throw 'Replay changed the database.' }
```

`init` loads the workflow schemas and disposition catalogue for a new database.
On reopen, it verifies the schema version and key. `ingest` reads
`leads/<workflow>/*.csv` and `events/*.jsonl`. The earlier `ingest-leads`
command still imports only CSVs; use a fresh database if those same named
drops were already imported with that command and you now need their events.

Each drop is one transaction. Its directory name identifies the delivery and
its input paths and bytes determine its fingerprint. An identical replay is a
no-op; a changed delivery using the same name is rejected. If multiple columns
match one field, the leftmost wins. Amounts use half-up rounding and accept
properly grouped Western or Indian thousands separators. Defaults are applied
after merging incoming non-null values.

The ledger keeps the first event for each ID. Its original JSON is encrypted
with AES-GCM, as are sensitive lead fields. The indexed phone lookup uses a
separate workflow-scoped HMAC. Only the last four digits are kept for masking.
An override has no phone; it links through sibling events for the same workflow
and call ID. Unlinked events stay in the ledger and are resolved against the
current leads when deriving call facts. No ledger row needs to be updated.

An invalid JSON line or missing/invalid string ID counts as malformed. After
deduplication, a unique event with invalid required metadata (workflow, call,
type, or timestamp) stops and rolls back the drop. A regular event with an
invalid phone stays orphaned, and an unknown disposition counts only after
linking. Naive timestamps use IST. Equal disposition times are broken by event
ID; if a call has several initiations, the earliest linked initiation wins.

Use the same `LEDGER_MASTER_KEY` when reopening a database. Keep it out of Git;
the starter ignore rules already cover `.venv`, `data`, `.sqlite3` files and
their sidecar files.
