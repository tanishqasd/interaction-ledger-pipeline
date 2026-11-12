"""The SQLite schema for source records and the append-only ledger."""

SCHEMA_VERSION = 1

SCHEMA = (
    """CREATE TABLE pipeline_meta (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        key_tag BLOB NOT NULL CHECK (length(key_tag) = 32)
    )""",
    """CREATE TABLE workflows (
        workflow_id TEXT PRIMARY KEY NOT NULL,
        schema_json TEXT NOT NULL
    )""",
    """CREATE TABLE dispositions (
        workflow_id TEXT NOT NULL REFERENCES workflows(workflow_id),
        code TEXT NOT NULL,
        label TEXT NOT NULL,
        is_terminal INTEGER NOT NULL CHECK (is_terminal IN (0, 1)),
        is_success INTEGER NOT NULL CHECK (is_success IN (0, 1)),
        PRIMARY KEY (workflow_id, code)
    )""",
    """CREATE TABLE ingested_drops (
        drop_id INTEGER PRIMARY KEY,
        source_key TEXT NOT NULL UNIQUE,
        manifest_digest BLOB NOT NULL CHECK (length(manifest_digest) = 32),
        lead_rows_total INTEGER NOT NULL DEFAULT 0 CHECK (lead_rows_total >= 0),
        duplicate_lead_rows_merged INTEGER NOT NULL DEFAULT 0 CHECK (duplicate_lead_rows_merged >= 0),
        field_warnings INTEGER NOT NULL DEFAULT 0 CHECK (field_warnings >= 0),
        events_total INTEGER NOT NULL DEFAULT 0 CHECK (events_total >= 0),
        malformed_events INTEGER NOT NULL DEFAULT 0 CHECK (malformed_events >= 0),
        duplicate_events_dropped INTEGER NOT NULL DEFAULT 0 CHECK (duplicate_events_dropped >= 0)
    )""",
    """CREATE TABLE batches (
        batch_pk INTEGER PRIMARY KEY,
        drop_id INTEGER NOT NULL REFERENCES ingested_drops(drop_id),
        workflow_id TEXT NOT NULL REFERENCES workflows(workflow_id),
        batch_id TEXT NOT NULL,
        UNIQUE (drop_id, workflow_id, batch_id),
        UNIQUE (batch_pk, workflow_id)
    )""",
    """CREATE TABLE leads (
        lead_id INTEGER PRIMARY KEY,
        workflow_id TEXT NOT NULL REFERENCES workflows(workflow_id),
        first_batch_pk INTEGER NOT NULL,
        phone_token BLOB NOT NULL CHECK (length(phone_token) = 32),
        phone_last4 TEXT NOT NULL CHECK (phone_last4 GLOB '[0-9][0-9][0-9][0-9]'),
        external_ref TEXT NOT NULL,
        attributes_json TEXT NOT NULL,
        pii_ciphertext BLOB NOT NULL,
        UNIQUE (workflow_id, phone_token),
        FOREIGN KEY (first_batch_pk, workflow_id) REFERENCES batches(batch_pk, workflow_id)
    )""",
    """CREATE TABLE rejections (
        batch_pk INTEGER NOT NULL REFERENCES batches(batch_pk),
        row_number INTEGER NOT NULL CHECK (row_number > 0),
        reason TEXT NOT NULL,
        PRIMARY KEY (batch_pk, row_number)
    )""",
    # Store timestamps as integer microseconds since the Unix epoch (UTC).
    # A null code keeps unknown dispositions out of metrics without losing the encrypted event.
    """CREATE TABLE events (
        event_id TEXT PRIMARY KEY NOT NULL,
        drop_id INTEGER NOT NULL REFERENCES ingested_drops(drop_id),
        workflow_id TEXT NOT NULL,
        call_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        occurred_at_utc INTEGER NOT NULL,
        phone_token BLOB CHECK (phone_token IS NULL OR length(phone_token) = 32),
        disposition_code TEXT,
        event_ciphertext BLOB NOT NULL,
        FOREIGN KEY (workflow_id, disposition_code) REFERENCES dispositions(workflow_id, code)
    )""",
    "CREATE INDEX events_by_phone ON events (workflow_id, phone_token)",
    "CREATE INDEX events_by_call ON events (workflow_id, call_id)",
    "CREATE INDEX leads_by_reference ON leads (workflow_id, external_ref)",
    """CREATE TRIGGER events_no_update BEFORE UPDATE ON events
       BEGIN SELECT RAISE(ABORT, 'event ledger is append-only'); END""",
    """CREATE TRIGGER events_no_delete BEFORE DELETE ON events
       BEGIN SELECT RAISE(ABORT, 'event ledger is append-only'); END""",
)
