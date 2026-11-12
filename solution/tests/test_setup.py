import base64
import os
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from solution.db import connect, initialize
from solution.keys import KEY_ENV

ROOT = Path(__file__).resolve().parents[2]
TEST_KEY = bytes(range(32))


class SetupTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Path(temp.name) / "ledger.sqlite3"

    def run_init(self, key: str | None) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.pop(KEY_ENV, None)
        if key is not None:
            env[KEY_ENV] = key
        return subprocess.run(
            [sys.executable, "-m", "solution", "init", "--db", str(self.db)],
            cwd=ROOT, env=env, capture_output=True, text=True, check=False,
        )

    def test_init_and_reopen_in_separate_processes(self):
        encoded = base64.b64encode(TEST_KEY).decode("ascii")
        created = self.run_init(encoded)
        self.assertEqual(created.returncode, 0, created.stderr)
        self.assertIn("Initialized ledger", created.stdout)

        before = self.db.read_bytes()
        reopened = self.run_init(encoded)
        self.assertEqual(reopened.returncode, 0, reopened.stderr)
        self.assertIn("Verified existing ledger", reopened.stdout)
        self.assertEqual(self.db.read_bytes(), before)

        wrong = self.run_init(base64.b64encode(bytes(32)).decode("ascii"))
        self.assertEqual(wrong.returncode, 1)
        self.assertIn("does not match this database", wrong.stderr)
        self.assertEqual(self.db.read_bytes(), before)

        with closing(connect(self.db)) as connection:
            workflows = connection.execute("SELECT workflow_id FROM workflows ORDER BY workflow_id")
            self.assertEqual([row[0] for row in workflows], ["motor_renewal", "policy_feedback"])
            successes = connection.execute(
                "SELECT code FROM dispositions WHERE is_success = 1 ORDER BY code"
            )
            self.assertEqual([row[0] for row in successes], ["feedback_captured", "renewed"])

    def test_missing_or_invalid_key_does_not_create_a_database(self):
        for key in (None, "not base64!", "eA=="):
            with self.subTest(key=key):
                result = self.run_init(key)
                self.assertEqual(result.returncode, 1)
                self.assertIn(KEY_ENV, result.stderr)
                self.assertFalse(self.db.exists())

    def test_failed_initialization_rolls_back_schema(self):
        with patch("solution.db._load_references", side_effect=OSError("cannot read catalogue")):
            with self.assertRaisesRegex(OSError, "cannot read catalogue"):
                initialize(self.db, TEST_KEY)
        with closing(connect(self.db)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertIsNone(connection.execute("SELECT 1 FROM sqlite_schema").fetchone())
        self.assertTrue(initialize(self.db, TEST_KEY))

    def test_foreign_keys_and_append_only_ledger(self):
        initialize(self.db, TEST_KEY)
        with closing(connect(self.db)) as connection, connection:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "FOREIGN KEY"):
                connection.execute(
                    "INSERT INTO batches (drop_id, workflow_id, batch_id) VALUES (1, ?, ?)",
                    ("motor_renewal", "batch_001"),
                )
            connection.execute(
                "INSERT INTO ingested_drops (source_key, manifest_digest) VALUES (?, ?)",
                ("drop_1", bytes(32)),
            )
            event_values = ("evt_1", 1, "motor_renewal", "call_1", "call.initiated", 0, b"test")
            columns = "(event_id, drop_id, workflow_id, call_id, event_type, occurred_at_utc, event_ciphertext)"
            insert = f"INTO events {columns} VALUES (?, ?, ?, ?, ?, ?, ?)"
            connection.execute(f"INSERT {insert}", event_values)
            for sql, args in (
                ("UPDATE events SET call_id = 'changed' WHERE event_id = 'evt_1'", ()),
                ("DELETE FROM events WHERE event_id = 'evt_1'", ()),
                (f"INSERT OR REPLACE {insert}", event_values),
            ):
                with self.subTest(sql=sql), self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                    connection.execute(sql, args)
            self.assertEqual(connection.execute("SELECT call_id FROM events").fetchone()[0], "call_1")
