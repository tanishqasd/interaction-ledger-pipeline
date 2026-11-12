import base64
import csv
import hashlib
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from solution.tests.report_contract import CSV_HEADERS, QUALITY_KEYS

ROOT = Path(__file__).resolve().parents[2]
MOTOR = "motor_renewal"
FEEDBACK = "policy_feedback"
NOW = "2026-10-01T18:35:00Z"  # 00:05 IST the next day


def event(event_id, kind, call_id, phone=None, *, when=NOW, code=None, workflow=MOTOR):
    result = {"event_id": event_id, "event_type": kind, "call_id": call_id,
              "workflow_id": workflow, "occurred_at": when, "payload": {}}
    if phone is not None:
        result["phone"] = phone
    if code is not None:
        result["payload"]["disposition_code"] = code
    return result


class ReportTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.db = self.root / "ledger #1.sqlite3"
        self.out = self.root / "reports"

    def run_cli(self, *args):
        env = os.environ.copy()
        env.pop("LEDGER_MASTER_KEY", None)
        if args[0] != "report":
            env["LEDGER_MASTER_KEY"] = base64.b64encode(bytes(range(32))).decode("ascii")
        return subprocess.run([sys.executable, "-m", "solution", *map(str, args)], cwd=ROOT,
                              env=env, capture_output=True, text=True, check=False)

    def cli(self, *args):
        result = self.run_cli(*args)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def write_leads(self, drop, workflow, batch, rows):
        path = self.root / drop / "leads" / workflow / f"{batch}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as file:
            reference = "Policy No" if workflow == MOTOR else "Order ID"
            csv.writer(file).writerows([["Name", "Mobile", reference, "Language"], *rows])

    def write_events(self, drop, rows):
        path = self.root / drop / "events" / "events_0001.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(row if isinstance(row, str) else json.dumps(row) for row in rows) + "\n",
                        encoding="utf-8")

    def assert_csv(self, filename, rows):
        self.assertEqual((self.out / filename).read_text(encoding="utf-8").splitlines(),
                         [CSV_HEADERS[filename], *rows])

    def quality(self):
        return json.loads((self.out / "data_quality.json").read_text(encoding="utf-8"))

    def report(self):
        before = self.db.read_bytes()
        self.cli("report", "--out", self.out, "--db", self.db)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual({path.name for path in self.out.iterdir()}, {*CSV_HEADERS, "data_quality.json"})

    def report_hashes(self):
        return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in self.out.iterdir()}

    def test_all_five_reports_match_hand_calculated_output_after_late_lead(self):
        primary, failed, dialed = "9876543210", "8765432109", "7654321098"
        quiet, late, feedback = "6543210987", "6123456789", "9988776655"
        name = "SYNTHETIC-CUSTOMER-NAME-NEVER-EXPORTED"
        note = "SYNTHETIC-EVENT-PAYLOAD-NEVER-EXPORTED"
        self.write_leads("drop_1", FEEDBACK, "batch_001", [[name, feedback, "ORDER-1"], [name, "", "ORDER-2"]])
        self.write_leads("drop_1", MOTOR, "batch_009", [[name, "123", "IGNORED"]] * 10)
        self.write_leads("drop_1", MOTOR, "batch_002", [[name, dialed, "M-DIALED"], [name, quiet, "B-QUIET"]])
        self.write_leads("drop_1", MOTOR, "batch_001", [
            [name, primary, "Z-OLD"], ["  ", " "], ["", "123", "IGNORED"],
            [name, failed, "A-FAILED", "bad language"], [name, "123", "IGNORED"],
        ])
        first = event("init-a1", "call.initiated", "a1", "+91 98765 43210")
        first["payload"]["internal_note"] = note
        self.write_events("drop_1", [
            event("override-a1", "disposition.override", "a1", code="renewed", when="2026-10-02 00:40:00"),
            event("override-d", "disposition.override", "d", code="callback_requested"),
            first,
            event("conn-a1", "call.connected", "a1", primary),
            event("set-a1", "disposition.set", "a1", primary, code="callback_requested", when="2026-10-02 00:50:00"),
            event("init-a2", "call.initiated", "a2", primary, when="2026-10-02 01:00:00"),
            event("conn-a2", "call.connected", "a2", primary, when="2026-10-01T19:34:00Z"),
            event("set-a2", "disposition.set", "a2", primary, code="renewed", when="2026-10-02 01:07:00"),
            event("init-a3", "call.initiated", "a3", primary, when="2026-10-03 10:00:00"),
            event("init-fail", "call.initiated", "fail", failed, when="2026-10-02 03:00:00"),
            event("fail", "call.telephony_failed", "fail", failed),
            event("unknown", "disposition.set", "fail", failed, code="not-in-catalogue"),
            event("init-d", "call.initiated", "d", dialed, when="2026-10-01T23:55:00+05:30"),
            event("set-d", "disposition.set", "d", dialed, code="renewed"),
            event("conn-no-init", "call.connected", "quiet", quiet),
            event("set-no-init", "disposition.set", "quiet", quiet, code="dnd"),
            event("init-late", "call.initiated", "late", late, when="2026-10-02 11:00:00"),
            event("conn-late", "call.connected", "late", late),
            event("set-late", "disposition.set", "late", late, code="renewed"),
            event("unknown-late", "disposition.set", "late", late, code="not-in-catalogue"),
            event("orphan", "call.initiated", "orphan", "6333333333"),
            event("init-feedback", "call.initiated", "feedback", feedback, workflow=FEEDBACK, when="2026-10-01 10:00:00"),
            event("conn-feedback", "call.connected", "feedback", feedback, workflow=FEEDBACK),
            event("set-feedback", "disposition.set", "feedback", feedback, code="feedback_captured", workflow=FEEDBACK),
            first, '{"event_id": ', " ",
        ])
        self.cli("init", "--db", self.db)
        self.cli("ingest", self.root / "drop_1", "--db", self.db)
        self.report()
        self.assert_csv("daily_overview.csv", [
            "motor_renewal,2026-10-01,1,0,0.0000,0",
            "motor_renewal,2026-10-02,3,2,0.6667,1",
            "motor_renewal,2026-10-03,1,0,0.0000,0",
            "policy_feedback,2026-10-01,1,1,1.0000,1",
        ])
        self.assertEqual(self.quality()["orphan_events"], 5)
        self.assertEqual(self.quality()["unknown_disposition_events"], 1)

        self.write_leads("drop_2", MOTOR, "batch_003", [[name, "09876543210", "Z-REVISED"], [name, late, "C-LATE"]])
        self.write_events("drop_2", [first])
        self.cli("ingest", self.root / "drop_2", "--db", self.db)
        self.report()
        self.assert_csv("batch_summary.csv", [
            "motor_renewal,batch_001,2,0,1,0,1",
            "motor_renewal,batch_002,2,1,0,1,0",
            "motor_renewal,batch_003,1,0,0,0,1",
            "policy_feedback,batch_001,1,0,0,0,1",
        ])
        self.assert_csv("daily_overview.csv", [
            "motor_renewal,2026-10-01,1,0,0.0000,0",
            "motor_renewal,2026-10-02,4,3,0.7500,2",
            "motor_renewal,2026-10-03,1,0,0.0000,0",
            "policy_feedback,2026-10-01,1,1,1.0000,1",
        ])
        self.assert_csv("lead_report.csv", [
            "motor_renewal,A-FAILED,batch_001,+91******2109,in_progress_telephony_failed,1,0,2026-10-02T03:00:00+05:30,,false",
            "motor_renewal,B-QUIET,batch_002,+91******0987,yet_to_reach_out,0,0,,,false",
            "motor_renewal,C-LATE,batch_003,+91******6789,completed,1,1,2026-10-02T11:00:00+05:30,renewed,true",
            "motor_renewal,M-DIALED,batch_002,+91******1098,in_progress_dialed,1,0,2026-10-01T23:55:00+05:30,callback_requested,false",
            "motor_renewal,Z-REVISED,batch_001,+91******3210,completed,3,2,2026-10-03T10:00:00+05:30,renewed,true",
            "policy_feedback,ORDER-1,batch_001,+91******6655,completed,1,1,2026-10-01T10:00:00+05:30,feedback_captured,true",
        ])
        self.assert_csv("rejections.csv", [
            "motor_renewal,batch_001,2,missing_required:customer_name",
            "motor_renewal,batch_001,4,invalid_phone",
            *(f"motor_renewal,batch_009,{row},invalid_phone" for row in range(1, 11)),
            "policy_feedback,batch_001,2,missing_required:phone",
        ])
        self.assertEqual(self.quality(), {
            "lead_rows_total": 20, "lead_rows_rejected": 13,
            "rejections_by_reason": {"invalid_phone": 11, "missing_required:customer_name": 1, "missing_required:phone": 1},
            "duplicate_lead_rows_merged": 1, "field_warnings": 1, "events_total": 27, "malformed_events": 1,
            "duplicate_events_dropped": 2, "orphan_events": 1, "unknown_disposition_events": 2,
        })
        for path in self.out.iterdir():
            content = path.read_text(encoding="utf-8")
            for sensitive in (name, note, primary, failed, dialed, quiet, late, feedback, "pii_ciphertext", "phone_token"):
                self.assertNotIn(sensitive, content)

        hashes = self.report_hashes()
        self.report()
        self.assertEqual(self.report_hashes(), hashes)
        self.assertEqual(self.cli("ingest", self.root / "drop_1", self.root / "drop_2", "--db", self.db)
                         .count("already imported; no changes"), 2)
        self.report()
        self.assertEqual(self.report_hashes(), hashes)

    def test_empty_database_emits_four_headers_and_zero_quality_without_key(self):
        self.cli("init", "--db", self.db)
        self.report()
        for filename in CSV_HEADERS:
            self.assert_csv(filename, [])
        self.assertEqual(self.quality(), {key: {} if key == "rejections_by_reason" else 0 for key in QUALITY_KEYS})

    def test_connectivity_rounds_half_to_even_at_four_places(self):
        self.write_leads("drop", MOTOR, "batch_001", [["Synthetic caller", "9876543210", "REF"]])
        self.write_events("drop", [
            *(event(f"init-{i}", "call.initiated", f"call-{i}", "9876543210") for i in range(32)),
            event("one-connected", "call.connected", "call-0", "9876543210"),
        ])
        self.cli("ingest", self.root / "drop", "--db", self.db)
        self.report()
        self.assert_csv("daily_overview.csv", ["motor_renewal,2026-10-02,32,1,0.0312,0"])

    def test_report_does_not_create_a_missing_database_or_overwrite_existing_database(self):
        missing = self.run_cli("report", "--out", self.out, "--db", self.db)
        self.assertEqual(missing.returncode, 1)
        self.assertFalse(self.db.exists())
        self.assertFalse(self.out.exists())

        self.out.mkdir()
        database_in_output = self.out / "lead_report.csv"
        self.cli("init", "--db", database_in_output)
        before = database_in_output.read_bytes()
        collision = self.run_cli("report", "--out", self.out, "--db", database_in_output)
        self.assertEqual(collision.returncode, 1)
        self.assertIn("overwrite the ledger database", collision.stderr)
        self.assertEqual(database_in_output.read_bytes(), before)
