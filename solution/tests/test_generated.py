import base64
import csv
import hashlib
import json
import os
import subprocess
import sys
import unittest
from collections import Counter
from contextlib import closing
from datetime import date, datetime, timedelta
from fractions import Fraction
from pathlib import Path
from tempfile import TemporaryDirectory

from solution.calls import derive_interactions
from solution.db import connect
from solution.keys import KEY_ENV
from solution.tests.report_contract import CSV_HEADERS, QUALITY_KEYS

ROOT = Path(__file__).resolve().parents[2]


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

    def check_reports(self, directory, expected_event_counts, expected_calls):
        tables = {}
        count_columns = {
            "batch_summary.csv": CSV_HEADERS["batch_summary.csv"].split(",")[2:],
            "daily_overview.csv": ("total_calls", "connected_calls", "success_leads"),
            "lead_report.csv": ("total_calls", "connected_calls"),
            "rejections.csv": ("row_number",),
        }
        for filename, header in CSV_HEADERS.items():
            with (directory / filename).open(encoding="utf-8", newline="") as file:
                reader = csv.DictReader(file)
                self.assertEqual(reader.fieldnames, header.split(","))
                tables[filename] = list(reader)
            for row in tables[filename]:
                self.assertNotIn(None, row)
                self.assertNotIn(None, row.values())
                for column in count_columns[filename]:
                    self.assertRegex(row[column], r"^(0|[1-9][0-9]*)$")

        batch = tables["batch_summary.csv"]
        daily = tables["daily_overview.csv"]
        leads = tables["lead_report.csv"]
        rejections = tables["rejections.csv"]
        for rows, key in (
            (batch, lambda row: (row["workflow_id"], row["batch_id"])),
            (daily, lambda row: (row["workflow_id"], row["date"])),
            (leads, lambda row: (row["workflow_id"], row["external_ref"])),
            (rejections, lambda row: (row["workflow_id"], row["batch_id"], int(row["row_number"]))),
        ):
            self.assertEqual(rows, sorted(rows, key=key))

        buckets = count_columns["batch_summary.csv"][1:]
        lead_groups = Counter((row["workflow_id"], row["batch_id"], row["bucket"]) for row in leads)
        self.assertEqual(sum(int(row["total_leads"]) for row in batch), len(leads))
        for row in batch:
            self.assertEqual(int(row["total_leads"]), sum(int(row[bucket]) for bucket in buckets))
            for bucket in buckets:
                self.assertEqual(int(row[bucket]), lead_groups[row["workflow_id"], row["batch_id"], bucket])

        self.assertEqual(sum(int(row["total_calls"]) for row in daily), expected_calls)
        self.assertEqual(sum(int(row["total_calls"]) for row in leads), expected_calls)
        self.assertEqual(sum(int(row["connected_calls"]) for row in daily),
                         sum(int(row["connected_calls"]) for row in leads))
        for row in daily:
            self.assertEqual(date.fromisoformat(row["date"]).isoformat(), row["date"])
            total, connected = int(row["total_calls"]), int(row["connected_calls"])
            self.assertGreater(total, 0)
            self.assertLessEqual(connected, total)
            self.assertLessEqual(int(row["success_leads"]), total)
            self.assertEqual(row["connectivity_rate"], f"{float(round(Fraction(connected, total), 4)):.4f}")
        for row in leads:
            self.assertRegex(row["phone_masked"], r"^\+91\*{6}[0-9]{4}$")
            self.assertIn(row["bucket"], buckets)
            self.assertIn(row["ever_success"], ("true", "false"))
            if int(row["total_calls"]) == 0:
                self.assertEqual((row["last_call_at"], row["current_disposition"], row["ever_success"]), ("", "", "false"))
            else:
                timestamp = datetime.fromisoformat(row["last_call_at"])
                self.assertEqual(timestamp.isoformat(), row["last_call_at"])
                self.assertEqual(timestamp.utcoffset(), timedelta(hours=5, minutes=30))

        quality = json.loads((directory / "data_quality.json").read_text(encoding="utf-8"))
        self.assertEqual(set(quality), set(QUALITY_KEYS))
        for key in QUALITY_KEYS:
            if key != "rejections_by_reason":
                self.assertIs(type(quality[key]), int)
                self.assertGreaterEqual(quality[key], 0)
        self.assertEqual(quality["rejections_by_reason"], Counter(row["reason"] for row in rejections))
        self.assertEqual(quality["lead_rows_rejected"], len(rejections))
        self.assertEqual(quality["lead_rows_total"], len(leads) + len(rejections) + quality["duplicate_lead_rows_merged"])
        for index, key in enumerate(("events_total", "malformed_events", "duplicate_events_dropped")):
            self.assertEqual(quality[key], sum(count[index] for count in expected_event_counts))

    def test_official_generator_sequential_and_bundled_ingest_on_two_seeds(self):
        for seed in (42, 2026):
            with self.subTest(seed=seed), TemporaryDirectory() as temp:
                directory = Path(temp)
                self.run_command("assignment-a-data/tools/generate_data.py", "--seed", seed,
                                 "--scale", "small", "--out", directory)
                drops = [directory / "drop_1", directory / "drop_2"]
                sequential_db, bundled_db = directory / "sequential.sqlite3", directory / "bundled.sqlite3"
                sequential_out, bundled_out = directory / "sequential_reports", directory / "bundled_reports"

                self.run_command("-m", "solution", "init", "--db", sequential_db)
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
                bundled_before = bundled_db.read_bytes()
                self.run_command("-m", "solution", "report", "--out", sequential_out, "--db", sequential_db)
                self.run_command("-m", "solution", "report", "--out", bundled_out, "--db", bundled_db)
                self.assertEqual(sequential_db.read_bytes(), before)
                self.assertEqual(bundled_db.read_bytes(), bundled_before)
                hashes = self.report_hashes(sequential_out)
                self.assertEqual(self.report_hashes(bundled_out), hashes)
                total_calls = sum(map(len, snapshot.calls_by_lead.values()))
                self.check_reports(sequential_out, expected_counts, total_calls)

                replay = self.run_command("-m", "solution", "ingest", *drops, "--db", sequential_db)
                self.assertEqual(replay.count("already imported; no changes"), 2)
                self.assertEqual(sequential_db.read_bytes(), before)
                self.run_command("-m", "solution", "report", "--out", sequential_out, "--db", sequential_db)
                self.assertEqual(self.report_hashes(sequential_out), hashes)
                self.assertEqual(sequential_db.read_bytes(), before)
                print(f"\nseed={seed}: {sum(count[0] for count in expected_counts)} event lines, "
                      f"{len(expected_ids)} ledger rows, {sum(count[1] for count in expected_counts)} malformed, "
                      f"{sum(count[2] for count in expected_counts)} duplicates, {snapshot.orphan_events} final orphans, "
                      f"{snapshot.unknown_disposition_events} unknown dispositions, "
                      f"{total_calls} calls; five reports validated, report hashes identical, database unchanged", flush=True)
