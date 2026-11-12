import base64
import csv
import hashlib
import json
import os
import subprocess
import sys
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

from solution.db import connect
from solution.keys import KEY_ENV
from solution.tests.generated_oracle import expected_reports
from solution.tests.report_contract import CSV_HEADERS, QUALITY_KEYS

ROOT = Path(__file__).resolve().parents[2]
ASSIGNMENT = ROOT / "assignment-a-data"


class GeneratedDataTests(unittest.TestCase):
    def run_command(self, *args):
        env = os.environ.copy()
        if args[:3] == ("-m", "solution", "report"):
            env.pop(KEY_ENV, None)
        else:
            env[KEY_ENV] = base64.b64encode(bytes(range(32))).decode("ascii")
        result = subprocess.run([sys.executable, *map(str, args)], cwd=ROOT, env=env,
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def report_hashes(self, directory):
        self.assertEqual({path.name for path in directory.iterdir()}, {*CSV_HEADERS, "data_quality.json"})
        return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in directory.iterdir()}

    def database_hashes(self, database):
        return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in database.parent.glob(database.name + "*") if not path.name.endswith("-shm")}

    def check_report_values(self, directory, expected):
        for filename, header in CSV_HEADERS.items():
            with (directory / filename).open(encoding="utf-8", newline="") as file:
                reader = csv.reader(file)
                self.assertEqual(next(reader), header.split(","), filename)
                self.assertEqual(list(reader), expected.rows[filename], filename)
        quality = json.loads((directory / "data_quality.json").read_text(encoding="utf-8"))
        self.assertEqual(tuple(quality), QUALITY_KEYS)
        self.assertEqual(quality, expected.quality)
        counts = [value for key, value in quality.items() if key != "rejections_by_reason"]
        self.assertTrue(all(type(value) is int for value in (*counts, *quality["rejections_by_reason"].values())))

    def test_official_generator_reports_against_raw_inputs_on_three_seeds(self):
        for seed in (42, 2026, 73129):
            with self.subTest(seed=seed), TemporaryDirectory() as temp:
                start = perf_counter()
                directory = Path(temp)
                self.run_command(ASSIGNMENT / "tools" / "generate_data.py", "--seed", seed,
                                 "--scale", "small", "--out", directory)
                drops = [directory / "drop_1", directory / "drop_2"]
                sequential = directory / "sequential.sqlite3"
                bundled = directory / "bundled.sqlite3"
                report = directory / "sequential_reports"
                bundled_report = directory / "bundled_reports"
                self.run_command("-m", "solution", "init", "--db", sequential)
                self.run_command("-m", "solution", "init", "--db", bundled)

                # Hold the WAL open while CLI processes ingest, read, and restart.
                with closing(connect(sequential)) as keeper:
                    self.assertEqual(keeper.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
                    for index, drop in enumerate(drops, start=1):
                        # Opening a read snapshot pins the WAL while the other process writes.
                        keeper.execute("BEGIN")
                        keeper.execute("SELECT COUNT(*) FROM events").fetchone()
                        self.run_command("-m", "solution", "ingest", drop, "--db", sequential)
                        expected = expected_reports(drops[:index], ASSIGNMENT / "schemas")
                        before = self.database_hashes(sequential)
                        self.assertIn("sequential.sqlite3-wal", before)
                        self.run_command("-m", "solution", "report", "--out", report, "--db", sequential)
                        self.assertEqual(self.database_hashes(sequential), before)
                        self.check_report_values(report, expected)
                        keeper.rollback()
                        self.assertEqual({row[0] for row in keeper.execute("SELECT event_id FROM events")},
                                         expected.event_ids)
                        for path in (sequential, sequential.with_suffix(".sqlite3-wal"), *report.iterdir()):
                            contents = path.read_bytes()
                            self.assertFalse(any(sample in contents for sample in expected.pii_samples),
                                             f"Unexpected plaintext from the input in {path.name}")
                        quality = expected.quality
                        print(f"\nseed={seed} drop={index}: {len(expected.rows['lead_report.csv'])} leads, "
                              f"{len(expected.event_ids)} events, {quality['orphan_events']} orphans, "
                              f"{quality['unknown_disposition_events']} unknown dispositions; "
                              "all five reports match raw-input expectations", flush=True)

                    self.assertEqual(keeper.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                    self.assertEqual(keeper.execute("PRAGMA foreign_key_check").fetchall(), [])
                    hashes = self.report_hashes(report)
                    replay = self.run_command("-m", "solution", "ingest", *drops, "--db", sequential)
                    self.assertEqual(replay.count("already imported; no changes"), 2)
                    self.run_command("-m", "solution", "report", "--out", report, "--db", sequential)
                    self.assertEqual(self.report_hashes(report), hashes)
                    self.assertEqual(self.database_hashes(sequential), before)

                self.run_command("-m", "solution", "ingest", *drops, "--db", bundled)
                bundled_before = self.database_hashes(bundled)
                self.run_command("-m", "solution", "report", "--out", bundled_report, "--db", bundled)
                self.assertEqual(self.database_hashes(bundled), bundled_before)
                self.assertEqual(self.report_hashes(bundled_report), hashes)
                print(f"seed={seed}: replay and bundled report hashes match; SQLite/WAL unchanged; "
                      f"PII sample scan passed ({perf_counter() - start:.2f}s)", flush=True)
