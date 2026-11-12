import base64
import json
import os
import subprocess
import sys
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from solution.calls import derive_interactions
from solution.db import connect
from solution.keys import KEY_ENV

ROOT = Path(__file__).resolve().parents[2]


class GeneratedDataTests(unittest.TestCase):
    def run_command(self, *args):
        env = {**os.environ, KEY_ENV: base64.b64encode(bytes(range(32))).decode("ascii")}
        result = subprocess.run([sys.executable, *map(str, args)], cwd=ROOT, env=env,
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_official_generator_sequential_and_bundled_ingest_on_two_seeds(self):
        for seed in (42, 2026):
            with self.subTest(seed=seed), TemporaryDirectory() as temp:
                directory = Path(temp)
                self.run_command("assignment-a-data/tools/generate_data.py", "--seed", seed,
                                 "--scale", "small", "--out", directory)
                drops = [directory / "drop_1", directory / "drop_2"]
                sequential_db, bundled_db = directory / "sequential.sqlite3", directory / "bundled.sqlite3"

                for drop in drops:
                    self.run_command("-m", "solution", "ingest", drop, "--db", sequential_db)
                self.run_command("-m", "solution", "ingest", *drops, "--db", bundled_db)

                expected_ids, expected_counts = set(), []
                for drop in drops:
                    total = malformed = duplicates = 0
                    for path in sorted((drop / "events").glob("*.jsonl")):
                        for line in path.read_text(encoding="utf-8").splitlines():
                            if not line.strip():
                                continue
                            total += 1
                            try:
                                event_id = json.loads(line)["event_id"]
                            except (KeyError, json.JSONDecodeError):
                                malformed += 1
                            else:
                                duplicates += event_id in expected_ids
                                expected_ids.add(event_id)
                    expected_counts.append((total, malformed, duplicates))

                with closing(connect(sequential_db)) as sequential, closing(connect(bundled_db)) as bundled:
                    counters = sequential.execute(
                        "SELECT events_total, malformed_events, duplicate_events_dropped FROM ingested_drops ORDER BY drop_id"
                    )
                    self.assertEqual([tuple(row) for row in counters], expected_counts)
                    self.assertEqual({row[0] for row in sequential.execute("SELECT event_id FROM events")}, expected_ids)
                    self.assertEqual(sequential.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                    self.assertEqual(sequential.execute("PRAGMA foreign_key_check").fetchall(), [])
                    snapshot = derive_interactions(sequential)
                    self.assertEqual(snapshot, derive_interactions(bundled))
                    self.assertGreater(len(snapshot.calls_by_lead), 0)
                    self.assertGreater(snapshot.orphan_events, 0)
                    self.assertGreater(snapshot.unknown_disposition_events, 0)

                before = sequential_db.read_bytes()
                replay = self.run_command("-m", "solution", "ingest", *drops, "--db", sequential_db)
                self.assertEqual(replay.count("already imported; no changes"), 2)
                self.assertEqual(sequential_db.read_bytes(), before)
                print(f"\nseed={seed}: {sum(count[0] for count in expected_counts)} event lines, "
                      f"{len(expected_ids)} ledger rows, {sum(count[1] for count in expected_counts)} malformed, "
                      f"{sum(count[2] for count in expected_counts)} duplicates, {snapshot.orphan_events} final orphans, "
                      f"{snapshot.unknown_disposition_events} unknown dispositions, "
                      f"{sum(map(len, snapshot.calls_by_lead.values()))} calls; replay identical", flush=True)
