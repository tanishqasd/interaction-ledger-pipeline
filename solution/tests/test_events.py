import base64
import csv
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from solution.calls import derive_interactions, lead_bucket
from solution.db import connect, initialize
from solution.events import parse_timestamp, timestamp_in_ist
from solution.ingest import ingest_drop
from solution.keys import KEY_ENV
from solution.privacy import Privacy

ROOT = Path(__file__).resolve().parents[2]
TEST_KEY = bytes(range(32))
MOTOR = "motor_renewal"
FEEDBACK = "policy_feedback"
PHONE = "(+91) 98765 43210"
NOW = "2026-10-01T18:35:00Z"


def event(event_id, kind, call_id="call_1", *, when=NOW, phone=PHONE, code=None, workflow=MOTOR):
    row = {"event_id": event_id, "event_type": kind, "call_id": call_id,
           "workflow_id": workflow, "occurred_at": when, "payload": {}}
    if phone is not None:
        row["phone"] = phone
    if code is not None:
        row["payload"]["disposition_code"] = code
    return row


class EventTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.db = self.root / "ledger.sqlite3"
        initialize(self.db, TEST_KEY)
        self.connection = connect(self.db)
        self.addCleanup(self.connection.close)
        self.privacy = Privacy(TEST_KEY)

    def write_events(self, drop, rows, filename="events_0001.jsonl"):
        path = self.root / drop / "events" / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\n".join(
            row if isinstance(row, bytes) else json.dumps(row).encode("utf-8") for row in rows
        ) + b"\n")
        return path

    def write_leads(self, drop, rows, workflow=MOTOR):
        path = self.root / drop / "leads" / workflow / "batch_001.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as file:
            csv.writer(file).writerows([
                ["Name", "Phone", "Policy No" if workflow == MOTOR else "Order"], *rows,
            ])

    def ingest(self, drop):
        return ingest_drop(self.connection, self.privacy, self.root / drop)

    def run_ingest(self, *drops):
        return subprocess.run(
            [sys.executable, "-m", "solution", "ingest", "--db", str(self.db),
             *(str(self.root / drop) for drop in drops)],
            cwd=ROOT, env={**os.environ, KEY_ENV: base64.b64encode(TEST_KEY).decode("ascii")},
            capture_output=True, text=True, check=False,
        )

    def test_timestamp_formats_and_ist_day_boundary(self):
        instants = [parse_timestamp(value) for value in (
            "2026-10-01T18:35:00Z", "2026-10-02T00:05:00+05:30",
            "2026-10-01T14:35:00-04:00", "2026-10-02 00:05:00",
        )]
        self.assertEqual(len(set(instants)), 1)
        self.assertEqual(timestamp_in_ist(instants[0]).isoformat(), "2026-10-02T00:05:00+05:30")
        self.assertEqual(parse_timestamp("2026-10-01T18:35:00.000001Z"), instants[0] + 1)
        for value in ("2026-10-01", "2026-02-30T18:35:00Z", "2026-10-01T18:35:00+05:99"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_timestamp(value)

    def test_malformed_and_duplicate_counts_across_files_drops_and_restarts(self):
        original = event("same-id", "call.initiated", phone="911234")
        self.write_events("drop_1", [original, original, b"", b" \t", b'{"event_id":"same-id",',
                         {}, {"event_id": ""}, {"event_id": 123}, ["not an event"], b"\xff",
                         b'{"event_id":"not-valid-json","number":NaN}'])
        self.write_events("drop_1", [{"event_id": "same-id"}], "events_0002.jsonl")
        self.write_events("drop_2", [event("same-id", "call.connected"), event("another", "call.initiated")])
        self.write_leads("drop_2", [["Test customer", PHONE, "POL-1"]])
        first = self.run_ingest("drop_1")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("10 event lines, 7 malformed, 2 duplicates", first.stdout)
        second = self.run_ingest("drop_2")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("2 event lines, 0 malformed, 1 duplicates", second.stdout)
        rows = self.connection.execute(
            "SELECT events_total, malformed_events, duplicate_events_dropped FROM ingested_drops ORDER BY drop_id"
        )
        self.assertEqual([tuple(row) for row in rows], [(10, 7, 2), (2, 0, 1)])
        saved = self.connection.execute("SELECT * FROM events WHERE event_id = 'same-id'").fetchone()
        self.assertIsNone(saved["phone_token"])
        self.assertEqual(json.loads(self.privacy.decrypt_event("same-id", saved["event_ciphertext"])), original)
        snapshot = derive_interactions(self.connection)
        self.assertEqual((snapshot.orphan_events, snapshot.unknown_disposition_events), (1, 0))
        self.assertEqual([call.call_id for calls in snapshot.calls_by_lead.values() for call in calls], ["call_1"])

        before = self.db.read_bytes()
        replay = self.run_ingest("drop_1", "drop_2")
        self.assertEqual(replay.returncode, 0, replay.stderr)
        self.assertEqual(replay.stdout.count("already imported; no changes"), 2)
        self.assertEqual(self.db.read_bytes(), before)
        self.write_events("drop_1", [event("changed", "call.initiated")])
        changed = self.run_ingest("drop_1")
        self.assertEqual(changed.returncode, 1)
        self.assertIn("different input files", changed.stderr)
        self.assertEqual(self.db.read_bytes(), before)

    def test_late_lead_relinks_orphans_and_override_without_rewriting_ledger(self):
        events = [
            event("override-latest", "disposition.override", phone=None, code="renewed", when="2026-10-02 00:09:00"),
            event("override-unknown", "disposition.override", phone=None, code="RENEWED", when="2026-10-02 01:09:00"),
            event("override-old", "disposition.override", phone=None, code="not_interested", when="2026-10-02T00:12:00+06:00"),
            event("agent-newer-than-overrides", "disposition.set", code="callback_requested", when="2026-10-01T18:43:00Z"),
            event("connected", "call.connected"),
            event("no-init", "call.connected", "not_a_call"),
            event("no-init-terminal", "disposition.set", "not_a_call", code="dnd"),
            event("started", "call.initiated"),
            event("telephony-failed", "call.telephony_failed"),
            event("second-start", "call.initiated", "call_2", when="2026-10-02T03:00:00+05:30"),
            event("second-failed", "call.telephony_failed", "call_2"),
            event("same-call-different-workflow", "disposition.override", phone=None,
                  code="feedback_captured", workflow=FEEDBACK),
        ]
        self.write_events("drop_1", events[:3])
        self.write_events("drop_1", events[3:], "events_0002.jsonl")
        self.ingest("drop_1")
        first = derive_interactions(self.connection)
        self.assertEqual((first.calls_by_lead, first.orphan_events, first.unknown_disposition_events), ({}, 12, 0))
        ledger = [tuple(row) for row in self.connection.execute("SELECT * FROM events ORDER BY event_id")]

        self.write_leads("drop_2", [["Late synthetic lead", "09876543210", "POL-1"]])
        self.ingest("drop_2")
        second = derive_interactions(self.connection)
        self.assertEqual((second.orphan_events, second.unknown_disposition_events), (1, 1))
        self.assertEqual(ledger, [tuple(row) for row in self.connection.execute("SELECT * FROM events ORDER BY event_id")])
        calls, = second.calls_by_lead.values()
        self.assertEqual([call.call_id for call in calls], ["call_1", "call_2"])
        self.assertEqual(calls[0].initiated_at_ist.isoformat(), "2026-10-02T00:05:00+05:30")
        self.assertTrue(calls[0].connected)
        self.assertFalse(calls[1].connected)
        self.assertIsNone(calls[1].disposition)
        self.assertTrue(all(call.telephony_failed for call in calls))
        outcome = calls[0].disposition
        self.assertEqual((outcome.code, outcome.is_terminal, outcome.is_success), ("renewed", True, True))
        self.assertEqual(lead_bucket(calls), "completed")

    def test_linked_calls_buckets_unknown_codes_and_deterministic_ties(self):
        failed, dialed, no_call = "8765432109", "7654321098", "6543210987"
        self.write_leads("drop", [["First", failed, "FAILED"], ["Second", dialed, "DIALED"],
                                  ["Third", no_call, "NONE"]])
        self.write_events("drop", [
            event("failed-init", "call.initiated", "failed", phone=failed),
            event("failed-init-later", "call.initiated", "failed", phone=failed, when="2026-10-02 00:08:00"),
            event("failed-flag", "call.telephony_failed", "failed", phone=failed),
            event("wrong-workflow-code", "disposition.set", "failed", phone=failed, code="feedback_captured"),
            event("ambiguous-override", "disposition.override", "failed", phone=None, code="renewed"),
            event("other-sibling-phone", "call.ended", "failed", phone="9999999999"),
            event("dialed-init", "call.initiated", "dialed", phone=dialed),
            event("z-wins", "disposition.set", "dialed", phone=dialed, code="callback_requested"),
            event("a-loses", "disposition.set", "dialed", phone=dialed, code="dnd", when="2026-10-02 00:05:00"),
            event("none-set", "disposition.set", "no-init", phone=no_call, code="dnd"),
            event("none-connected", "call.connected", "no-init", phone=no_call),
        ])
        self.ingest("drop")
        snapshot = derive_interactions(self.connection)
        self.assertEqual((snapshot.orphan_events, snapshot.unknown_disposition_events), (2, 1))
        calls_by_ref = {
            row["external_ref"]: snapshot.calls_by_lead.get(row["lead_id"], [])
            for row in self.connection.execute("SELECT lead_id, external_ref FROM leads")
        }
        self.assertEqual({key: lead_bucket(calls) for key, calls in calls_by_ref.items()}, {
            "FAILED": "in_progress_telephony_failed", "DIALED": "in_progress_dialed", "NONE": "yet_to_reach_out",
        })
        failed_call, = calls_by_ref["FAILED"]
        self.assertEqual(failed_call.initiated_at_ist.isoformat(), "2026-10-02T00:05:00+05:30")
        self.assertIsNone(failed_call.disposition)
        self.assertEqual(calls_by_ref["DIALED"][0].disposition.code, "callback_requested")

    def test_event_pii_is_encrypted_and_invalid_metadata_rolls_back_whole_drop(self):
        payload_secret = "SYNTHETIC-EVENT-PAYLOAD-NEVER-PLAINTEXT"
        good = event("good", "call.initiated")
        good["payload"]["customer_name"] = payload_secret
        bad = event("bad", "call.ended", when=payload_secret)
        self.write_leads("drop", [["SYNTHETIC-LEAD-CUSTOMER", PHONE, "POL-1"], ["", "bad", ""]])
        self.write_events("drop", [good, bad])
        before = self.db.read_bytes()
        failed = self.run_ingest("drop")
        self.assertEqual(failed.returncode, 1)
        self.assertIn("events_0001.jsonl:2: invalid occurred_at timestamp", failed.stderr)
        self.assertNotIn(payload_secret, failed.stdout + failed.stderr)
        self.assertNotIn(PHONE, failed.stdout + failed.stderr)
        self.assertEqual(self.db.read_bytes(), before)

        self.connection.execute("PRAGMA journal_mode = WAL")
        self.write_events("drop", [good])
        self.ingest("drop")
        row = self.connection.execute("SELECT phone_token, event_ciphertext FROM events").fetchone()
        self.assertEqual(row["phone_token"], self.privacy.phone_token(MOTOR, "+919876543210"))
        self.assertEqual(json.loads(self.privacy.decrypt_event("good", row["event_ciphertext"])), good)
        self.assertTrue(self.db.with_suffix(".sqlite3-wal").is_file())
        for path in self.root.glob("ledger.sqlite3*"):
            data = path.read_bytes()
            for secret in (payload_secret, PHONE, "+919876543210", "9876543210", "SYNTHETIC-LEAD-CUSTOMER"):
                self.assertNotIn(secret.encode("utf-8"), data)

    def test_same_call_id_in_two_workflows_keeps_overrides_separate(self):
        self.write_leads("drop", [["Synthetic lead", PHONE, "MOTOR-REF"]], workflow=MOTOR)
        self.write_leads("drop", [["Synthetic lead", PHONE, "FEEDBACK-REF"]], workflow=FEEDBACK)
        self.write_events("drop", [
            event("motor-override", "disposition.override", phone=None, code="renewed"),
            event("motor-init", "call.initiated"),
            event("feedback-init", "call.initiated", workflow=FEEDBACK),
            event("feedback-set", "disposition.set", code="partial_feedback", workflow=FEEDBACK),
        ])
        self.ingest("drop")
        snapshot = derive_interactions(self.connection)
        self.assertEqual(snapshot.orphan_events, 0)
        self.assertEqual(snapshot.unknown_disposition_events, 0)
        by_workflow = {row["workflow_id"]: snapshot.calls_by_lead[row["lead_id"]][0].disposition.code
                       for row in self.connection.execute("SELECT workflow_id, lead_id FROM leads")}
        self.assertEqual(by_workflow, {MOTOR: "renewed", FEEDBACK: "partial_feedback"})

    def test_process_exit_mid_drop_recovers_and_allows_retry(self):
        self.write_leads("drop", [["Synthetic lead", PHONE, "POL-1"]])
        self.write_events("drop", [event("original", "call.initiated")])
        worker = """
import os
import sys
from pathlib import Path
from solution import ingest
from solution.db import connect
from solution.privacy import Privacy

original = ingest.ingest_event_file

def abort_after_events(*args, **kwargs):
    original(*args, **kwargs)
    os._exit(37)

ingest.ingest_event_file = abort_after_events
ingest.ingest_drop(connect(Path(sys.argv[1])), Privacy(bytes(range(32))), Path(sys.argv[2]))
"""
        crashed = subprocess.run([sys.executable, "-c", worker, str(self.db), str(self.root / "drop")],
                                 cwd=ROOT, capture_output=True, text=True, check=False)
        self.assertEqual(crashed.returncode, 37, crashed.stderr)
        for table in ("ingested_drops", "leads", "events"):
            self.assertEqual(self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
        retried = self.run_ingest("drop")
        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assertEqual(self.connection.execute("SELECT event_id FROM events").fetchone()[0], "original")
        self.assertIsNone(self.ingest("drop"))
