import base64
import csv
import json
import os
import shutil
import subprocess
import sys
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from cryptography.exceptions import InvalidTag

from solution.db import connect, initialize
from solution.ingest import ingest_lead_drop
from solution.keys import KEY_ENV
from solution.leads import LeadInputError
from solution.privacy import Privacy

ROOT = Path(__file__).resolve().parents[2]
TEST_KEY = bytes(range(32))
MOTOR = "motor_renewal"
FEEDBACK = "policy_feedback"
PHONE = "+919876543210"
SECOND_PHONE = "+918765432109"
NAME = "SYNTHETIC-PERSON-1042"
EDITED_NAME = "SYNTHETIC-PERSON-2042"
OTHER_NAME = "SYNTHETIC-PERSON-3042"


class LeadIngestionTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.db = self.root / "ledger.sqlite3"
        self.privacy = Privacy(TEST_KEY)
        initialize(self.db, TEST_KEY)
        self.connection = connect(self.db)
        self.addCleanup(self.connection.close)

    def write_batch(self, drop, workflow, filename, rows):
        path = self.root / drop / "leads" / workflow / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as file:
            csv.writer(file).writerows(rows)
        return path

    def run_ingest(self, *drops):
        env = os.environ.copy()
        env[KEY_ENV] = base64.b64encode(TEST_KEY).decode("ascii")
        return subprocess.run(
            [sys.executable, "-m", "solution", "ingest-leads", "--db", str(self.db),
             *(str(self.root / drop) for drop in drops)],
            cwd=ROOT, env=env, capture_output=True, text=True, check=False,
        )

    def import_drop(self, drop):
        return ingest_lead_drop(self.connection, self.privacy, self.root / drop)

    def lead(self, workflow, phone):
        return self.connection.execute(
            """SELECT l.*, b.batch_id FROM leads l JOIN batches b ON l.first_batch_pk = b.batch_pk
               WHERE l.workflow_id = ? AND l.phone_token = ?""",
            (workflow, self.privacy.phone_token(workflow, phone)),
        ).fetchone()

    def test_validation_warnings_defaults_and_merges_in_file_and_drop_order(self):
        # Create the later filename first; its order on disk must not decide batch ownership.
        self.write_batch("drop_1", MOTOR, "batch_002.csv", [
            ["Insured Name", "Mobile No", "Policy No", "Expiry", "Premium", "Language"],
            ["Intermediate name", "09876543210", "POL-2", "31/02/2026", "", ""],
            [EDITED_NAME, "(+91) 98765 43210", "POL-3", "", "Rs. 15.005", ""],
        ])
        self.write_batch("drop_1", MOTOR, "batch_001.csv", [
            ["Language", "  Customer__NAME  ", " MOBILE_\tNo ", "Policy No", "expiry_date", "Premium", "ignored"],
            [" HI ", f"  {NAME}  ", "91-98765-43210", "POL-1", "01/02/2026", "₹9,999.995", "note"],
            [], ["  ", "\t", "  "],
            ["bad language", "", "bad phone", "", "impossible", "bad amount"],
            ["", "Name but no valid phone", "123", ""],
            ["", "Name but no phone", "", "POL-X"],
            ["", "Name but no reference", "9876543211", "  "],
            ["Klingon", OTHER_NAME, "8765432109", "POL-4", "31/02/2026", "NaN"],
            ["", "", "", "", "", "", "nonblank unknown column"],
        ])
        self.write_batch("drop_1", FEEDBACK, "batch_001.csv", [
            ["Order", "Contact", "Name", "Item", "Purchase Date"],
            [" ORDER-1 ", "9876543210", OTHER_NAME, "  Phone  ", "01-dEc-2026"],
        ])
        stats = self.import_drop("drop_1")
        self.assertEqual((stats.total, stats.rejected, stats.merged, stats.warnings), (10, 5, 2, 4))
        rejections = self.connection.execute(
            """SELECT b.workflow_id, b.batch_id, r.row_number, r.reason
               FROM rejections r JOIN batches b ON r.batch_pk = b.batch_pk ORDER BY r.row_number"""
        )
        self.assertEqual([tuple(row) for row in rejections], [
            (MOTOR, "batch_001", 2, "missing_required:customer_name"),
            (MOTOR, "batch_001", 3, "invalid_phone"),
            (MOTOR, "batch_001", 4, "missing_required:phone"),
            (MOTOR, "batch_001", 5, "missing_required:policy_number"),
            (MOTOR, "batch_001", 7, "missing_required:customer_name"),
        ])
        lead = self.lead(MOTOR, PHONE)
        self.assertEqual((lead["batch_id"], lead["external_ref"], lead["phone_last4"]),
                         ("batch_001", "POL-3", "3210"))
        self.assertEqual(json.loads(lead["attributes_json"]), {
            "preferred_language": "hi", "policy_expiry": "2026-02-01", "premium_amount": "15.01",
        })
        second = self.lead(MOTOR, SECOND_PHONE)
        self.assertEqual(json.loads(second["attributes_json"]), {
            "preferred_language": "en", "policy_expiry": None, "premium_amount": None,
        })
        feedback = self.lead(FEEDBACK, PHONE)
        self.assertEqual(feedback["external_ref"], "ORDER-1")
        self.assertEqual(json.loads(feedback["attributes_json"]), {
            "preferred_language": "en", "order_date": "2026-12-01", "product": "Phone",
        })

        self.write_batch("drop_2", MOTOR, "batch_003.csv", [
            ["Language", "Insured Name", "Policy No", "Mobile No", "Expiry Date", "Premium"],
            ["TA", NAME, "POL-5", "9876543210", "04-12-2026", ""],
            ["kn", OTHER_NAME, "POL-6", "8765432109", "", ""],
        ])
        stats = self.import_drop("drop_2")
        self.assertEqual((stats.total, stats.rejected, stats.merged, stats.warnings), (2, 0, 2, 0))
        updated = self.lead(MOTOR, PHONE)
        self.assertEqual((updated["lead_id"], updated["batch_id"], updated["external_ref"]),
                         (lead["lead_id"], "batch_001", "POL-5"))
        self.assertEqual(json.loads(updated["attributes_json"]), {
            "preferred_language": "ta", "policy_expiry": "2026-12-04", "premium_amount": "15.01",
        })
        self.assertEqual(json.loads(self.lead(MOTOR, SECOND_PHONE)["attributes_json"])["preferred_language"], "kn")
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM leads").fetchone()[0], 3)

    def test_restart_replay_and_changed_delivery_leave_database_unchanged(self):
        path = self.write_batch("drop_1", MOTOR, "batch_001.csv", [
            ["Name", "Phone", "Policy No", "Language"],
            [NAME, PHONE, "POL-1", "HI"],
            [NAME, PHONE, "POL-1", "bad language"],
            ["", "123", ""],
        ])
        first = self.run_ingest("drop_1")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("3 lead rows, 1 rejected, 1 merged, 1 field warnings", first.stdout)
        before = self.db.read_bytes()
        shutil.copytree(self.root / "drop_1", self.root / "relocated" / "drop_1")
        retry = self.run_ingest("relocated/drop_1")
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertIn("already imported; no changes", retry.stdout)
        self.assertEqual(self.db.read_bytes(), before)

        with path.open("a", encoding="utf-8", newline="") as file:
            csv.writer(file).writerow([EDITED_NAME, PHONE, "POL-2", "ta"])
        changed = self.run_ingest("drop_1")
        self.assertEqual(changed.returncode, 1)
        self.assertIn("already imported with different lead files", changed.stderr)
        self.assertEqual(self.db.read_bytes(), before)

    def test_malformed_later_batch_rolls_back_the_entire_drop(self):
        self.write_batch("drop_1", MOTOR, "batch_001.csv", [
            ["Name", "Phone", "Policy No"], [NAME, PHONE, "POL-1"], ["", "bad", ""],
        ])
        malformed = self.write_batch("drop_1", MOTOR, "batch_002.csv", [["Name", "Phone", "Policy No"]])
        with malformed.open("ab") as file:
            file.write(b'"unterminated')
        before = self.db.read_bytes()
        with self.assertRaisesRegex(LeadInputError, "Unreadable CSV: motor_renewal/batch_002.csv"):
            self.import_drop("drop_1")
        self.assertEqual(self.db.read_bytes(), before)

        self.write_batch("drop_1", MOTOR, "batch_002.csv", [
            ["Name", "Phone", "Policy No"], [EDITED_NAME, PHONE, "POL-2"],
        ])
        stats = self.import_drop("drop_1")
        self.assertEqual((stats.total, stats.rejected, stats.merged), (3, 1, 1))

    def test_pii_is_encrypted_and_lookup_is_scoped_to_workflow_and_key(self):
        self.assertEqual(self.connection.execute("PRAGMA journal_mode = WAL").fetchone()[0], "wal")
        for workflow, reference in ((MOTOR, "Policy No"), (FEEDBACK, "Order")):
            self.write_batch("drop_1", workflow, "batch_001.csv", [
                ["Name", "Phone", reference, "Notes"],
                [NAME, "91-98765-43210", "REF-1", "SYNTHETIC-NOTE-NEVER-STORED"],
                [EDITED_NAME, PHONE, "REF-2", ""],
            ])
        self.import_drop("drop_1")
        lead = self.lead(MOTOR, PHONE)
        token, blob = lead["phone_token"], lead["pii_ciphertext"]
        other_token = self.lead(FEEDBACK, PHONE)["phone_token"]
        self.assertNotEqual(token, other_token)
        self.assertNotEqual(token, Privacy(bytes(32)).phone_token(MOTOR, PHONE))
        self.assertEqual(self.privacy.decrypt_lead(MOTOR, token, blob), {
            "customer_name": EDITED_NAME, "phone": PHONE,
        })
        with self.assertRaises(InvalidTag):
            self.privacy.decrypt_lead(FEEDBACK, other_token, blob)
        with self.assertRaises(InvalidTag):
            self.privacy.decrypt_lead(MOTOR, token, blob[:-1] + bytes([blob[-1] ^ 1]))

        with closing(connect(self.db)) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        self.assertEqual(integrity, "ok")
        self.assertTrue(self.db.with_suffix(".sqlite3-wal").is_file())
        for database_file in self.root.glob("ledger.sqlite3*"):
            contents = database_file.read_bytes()
            for plaintext in (NAME, EDITED_NAME, PHONE, "9876543210", "91-98765-43210",
                              "SYNTHETIC-NOTE-NEVER-STORED"):
                with self.subTest(file=database_file.name, plaintext=plaintext):
                    self.assertNotIn(plaintext.encode(), contents)
