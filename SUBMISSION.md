# Submission

## Candidate

- **Name:** Tanishqa Dayma
- **Assignment chosen:** A — Interaction Ledger & Reporting Pipeline
- **Time spent:** Approximately 7 hours (candidate estimate for active work and review).

## How to run
Run from the root of a clean clone with Python 3.11 or newer. Use a fresh database for this example.

```powershell
python -m venv .venv
$ledgerPython = '.\.venv\Scripts\python.exe'
& $ledgerPython -m pip install -r solution/requirements.txt
$env:LEDGER_MASTER_KEY = (& $ledgerPython -c "import base64, secrets; print(base64.b64encode(secrets.token_bytes(32)).decode('ascii'))")
& $ledgerPython assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data
& $ledgerPython -m solution init --db ledger.sqlite3
& $ledgerPython -m solution ingest data/drop_1 --db ledger.sqlite3
& $ledgerPython -m solution report --out reports/drop_1 --db ledger.sqlite3
& $ledgerPython -m solution ingest data/drop_2 --db ledger.sqlite3
& $ledgerPython -m solution report --out reports/final --db ledger.sqlite3
& $ledgerPython -m unittest discover -s solution/tests -v
```

Use the same key when ingesting into an existing database. Keep it out of Git; reporting can run without it.

## Design

The CLI reads workflow schemas and the disposition catalogue into SQLite. SQLite supports constraints, transactions and consistent report snapshots without a separate service.

- `ingested_drops` records each source name, keyed manifest digest and ingestion counters; `batches` records the workflow and source filename for each upload.
- `leads` is unique on `(workflow_id, phone_token)`. A foreign key records its first batch. A valid duplicate updates only incoming non-null fields; defaults are applied after the merge. `rejections` stores the batch, nonblank row number and first failure reason.
- `events` has a global primary key on `event_id` and triggers that reject updates and deletes. It stores an encrypted original plus the fields needed to derive calls. Reports resolve leads from the current snapshot, so a late upload can link an earlier event. A supervisor override is a new event, linked by workflow and call ID.
- `pipeline_meta` verifies the ingestion key for an existing database. The random 32-byte key stays outside SQLite. HKDF derives separate keys for AES-GCM, phone lookup HMAC and manifest HMAC. The report only uses the phone suffix and the schema's non-PII reference; it never decrypts records.

## Decisions & assumptions
| Decision | Why | Alternative considered |
|---|---|---|
| A drop is an immutable named delivery; fingerprint paths and bytes. | A retry is a no-op even when the directory is moved. The same name with changed files is rejected. | Track only filenames, which would miss a changed delivery. |
| Link at report time; scope overrides to a workflow and require an unambiguous sibling phone. | Earlier events can link later without rewriting the ledger or assigning ambiguous overrides to the wrong lead. | Maintain and repair a separate link table. |
| Break equal disposition timestamps by event ID and equal call times by call ID; use the earliest linked initiation if a call has several. | Makes outputs independent of event arrival order where the spec leaves ties open. | Prefer file order for ties. |
| Use the leftmost matching CSV column, half-up premium rounding, and half-even connectivity rounding. | These are deterministic choices for unspecified cases. Properly grouped Western and Indian thousands separators are accepted. | Reject duplicate headers or use a single rounding rule for both measures. |
| Include batches that own at least one lead and list only observed rejection reasons. | Batch totals count first-owned leads; rejections have their own report. | Include zero-lead batch rows and zero-count reasons. |

## Failure modes considered

Malformed JSON or a missing/nonstring ID is counted and skipped. A first-seen event with unusable required metadata, or an unreadable CSV, rolls back its drop. A bad phone on a regular event leaves it orphaned; an unknown disposition is counted only after the event links. Reporting never writes to the database. Ingesting with a missing or incorrect key fails before any drop is written. A retry after an interrupted transaction can safely reprocess the drop.

Keys are not rotated automatically. Reporting derives call facts from the full ledger, and ingestion keeps the current drop's files in memory. I did not attempt the optional large-data performance target.

## Testing

The suite covers field normalization, merges and defaults, rejected rows, duplicate and orphan events, ambiguous overrides, timezone boundaries, buckets, crash and restart recovery, encryption, and exact report rows. A separate test calculation reads the official raw files and checks every report after Drop 1 and after both drops on seeds 42, 2026 and 73129. The suite also checks SQLite integrity, sampled input PII in SQLite/WAL and reports, and byte-identical report hashes after replay and bundled ingestion.

## Results

On Windows, all 23 tests passed on Python 3.14.10 (134.258 s) and in an isolated clone on Python 3.11.18 (97.055 s). A fresh dependency install succeeded on Python 3.11.18. In that clone, the documented seed-42 workflow (generator, initialization, both imports and both report runs) took 9.345 seconds. It ended with 1,892 leads, 8,500 ledger events and 462 orphans. The orphan count was 1,123 after Drop 1; the later upload resolved many earlier events even as more events arrived.

## If I had another day

1. Measure memory, ingest time and report time on the official large dataset. Stream input files or reduce the report query's working set if the measurements warrant it.
2. Add a documented key rotation and recovery procedure.
3. Add operational alerts for failed drops, rising orphan rates and changes to source files.
