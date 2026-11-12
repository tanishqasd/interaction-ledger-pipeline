"""Calculate expected reports from the official generator's raw CSV and JSONL.

This test-only oracle uses plain phones and input rows in memory. It never
calls the implementation, reads its database, or writes unmasked data.
"""

import csv
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
BUCKETS = ("yet_to_reach_out", "in_progress_telephony_failed", "in_progress_dialed", "completed")


@dataclass
class ExpectedReports:
    rows: dict[str, list[list[str]]]
    quality: dict
    event_ids: set[str]
    pii_samples: set[bytes]


def _phone(raw: str) -> str | None:
    digits = re.sub(r"[^0-9]", "", raw)
    match = re.fullmatch(r"(?:91|0)?([6-9][0-9]{9})", digits)
    return match[1] if match else None


def _header(value: str) -> str:
    return " ".join(value.lower().replace("_", " ").split())


def _optional_is_valid(field: dict, value: str) -> bool:
    if field["type"] == "enum":
        return value.lower() in field["values"]
    if field["type"] == "date":
        for pattern in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d-%b-%Y"):
            try:
                datetime.strptime(value, pattern)
                return True
            except ValueError:
                continue
        return False
    if field["type"] == "decimal":
        try:
            number = Decimal(re.sub(r"^(₹|rs\.?|inr)\s*", "", value, flags=re.I).replace(",", ""))
            return number.is_finite() and number >= 0
        except InvalidOperation:
            return False
    return True


def _lead_rows(drops, schemas):
    leads, rejections, samples = {}, [], set()
    total = merged = warnings = 0
    for drop in drops:
        for path in sorted((drop / "leads").glob("*/*.csv")):
            workflow = path.parent.name
            fields = schemas[workflow]
            aliases = {_header(alias): field["name"] for field in fields
                       for alias in [field["name"], *field.get("aliases", [])]}
            reference_field = next(field["name"] for field in fields if field.get("reference"))
            with path.open(encoding="utf-8", newline="") as source:
                nonblank = (row for row in csv.DictReader(source) if any(cell.strip() for cell in row.values()))
                for number, row in enumerate(nonblank, start=1):
                    total += 1
                    values = {aliases.get(_header(name)): raw.strip() for name, raw in row.items()}
                    phone = _phone(values.get("phone", ""))
                    failures = []
                    for field in fields:
                        name = field["name"]
                        if field["required"] and not values.get(name):
                            failures.append(f"missing_required:{name}")
                        elif name == "phone" and phone is None:
                            failures.append("invalid_phone")
                    if failures:
                        rejections.append((workflow, path.stem, number, failures[0]))
                        continue
                    warnings += sum(bool(values.get(field["name"])) and
                                    not _optional_is_valid(field, values[field["name"]])
                                    for field in fields if not field["required"])
                    identity = (workflow, phone)
                    merged += identity in leads
                    original_batch = leads[identity][0] if identity in leads else path.stem
                    leads[identity] = (original_batch, values[reference_field])
                    if number <= 12:
                        samples.update(secret.encode("utf-8") for secret in
                                       (values["customer_name"], values["phone"], phone, "+91" + phone))
    return leads, sorted(rejections), samples, (total, merged, warnings)


def _event_rows(drops):
    events = {}
    total = malformed = duplicates = 0
    for drop in drops:
        for path in sorted((drop / "events").glob("*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                total += 1
                try:
                    event = json.loads(line)
                    event_id = event["event_id"]
                except (KeyError, json.JSONDecodeError):
                    malformed += 1
                else:
                    if event_id in events:
                        duplicates += 1
                    else:
                        events[event_id] = event
    return events, (total, malformed, duplicates)


def _ist(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(IST) if parsed.tzinfo else parsed.replace(tzinfo=IST)


def _linked_calls(leads, events, catalogue):
    phones, groups = defaultdict(set), defaultdict(list)
    for event in events.values():
        phone = _phone(event.get("phone", ""))
        if phone and event["event_type"] != "disposition.override":
            phones[event["workflow_id"], event["call_id"]].add(phone)
    orphans = unknown = 0
    for event in events.values():
        workflow, call_id, kind = (event[key] for key in ("workflow_id", "call_id", "event_type"))
        if kind == "disposition.override":
            siblings = phones[workflow, call_id]
            phone = next(iter(siblings)) if len(siblings) == 1 else None
        else:
            phone = _phone(event.get("phone", ""))
        if (workflow, phone) not in leads:
            orphans += 1
        elif kind.startswith("disposition.") and (workflow, event["payload"].get("disposition_code")) not in catalogue:
            unknown += 1
        else:
            groups[(workflow, phone), call_id].append(event)

    calls = defaultdict(list)
    for (identity, call_id), group in groups.items():
        by_kind = defaultdict(list)
        for event in group:
            by_kind[event["event_type"]].append(event)
        if not by_kind["call.initiated"]:
            continue
        started = min(_ist(event["occurred_at"]) for event in by_kind["call.initiated"])
        effective = max(by_kind["disposition.override"] or by_kind["disposition.set"], default=None,
                        key=lambda event: (_ist(event["occurred_at"]), event["event_id"]))
        code = effective["payload"]["disposition_code"] if effective else ""
        terminal, success = catalogue.get((identity[0], code), (False, False))
        calls[identity].append((started, call_id, bool(by_kind["call.connected"]),
                                bool(by_kind["call.telephony_failed"]), code, terminal, success))
    return calls, orphans, unknown


def _call_rows(leads, calls):
    batches, days, lead_rows = defaultdict(Counter), defaultdict(list), []
    for identity, (batch, reference) in sorted(leads.items(), key=lambda item: (item[0][0], item[1][1])):
        workflow, phone = identity
        attempts = sorted(calls[identity])
        if not attempts:
            bucket = BUCKETS[0]
        elif any(call[5] for call in attempts):
            bucket = BUCKETS[3]
        elif all(call[3] for call in attempts):
            bucket = BUCKETS[1]
        else:
            bucket = BUCKETS[2]
        batches[workflow, batch][bucket] += 1
        for started, _, connected, _, _, _, success in attempts:
            days[workflow, started.date().isoformat()].append((connected, identity if success else None))
        dispositions = [call[4] for call in attempts if call[4]]
        lead_rows.append((workflow, reference, batch, "+91******" + phone[-4:], bucket, len(attempts),
                          sum(call[2] for call in attempts), attempts[-1][0].isoformat() if attempts else "",
                          dispositions[-1] if dispositions else "", str(any(call[6] for call in attempts)).lower()))
    daily = []
    for key, attempts in sorted(days.items()):
        connected = sum(call[0] for call in attempts)
        rate = round(Fraction(connected, len(attempts)), 4)
        daily.append((*key, len(attempts), connected, f"{float(rate):.4f}",
                      len({identity for _, identity in attempts if identity is not None})))
    return {
        "batch_summary.csv": [(*key, sum(counts.values()), *(counts[bucket] for bucket in BUCKETS))
                              for key, counts in sorted(batches.items())],
        "daily_overview.csv": daily,
        "lead_report.csv": lead_rows,
    }


def expected_reports(drops: list[Path], schema_dir: Path) -> ExpectedReports:
    schemas = {schema["workflow_id"]: schema["fields"] for schema in
               (json.loads(path.read_text(encoding="utf-8")) for path in schema_dir.glob("*.json"))}
    with (schema_dir / "dispositions.csv").open(encoding="utf-8", newline="") as source:
        catalogue = {(row["workflow_id"], row["code"]): (row["is_terminal"] == "true", row["is_success"] == "true")
                     for row in csv.DictReader(source)}
    leads, rejections, samples, (total, merged, warnings) = _lead_rows(drops, schemas)
    events, (event_total, malformed, duplicates) = _event_rows(drops)
    calls, orphans, unknown = _linked_calls(leads, events, catalogue)
    tables = _call_rows(leads, calls)
    tables["rejections.csv"] = rejections
    return ExpectedReports({name: [list(map(str, row)) for row in rows] for name, rows in tables.items()}, {
        "lead_rows_total": total, "lead_rows_rejected": len(rejections),
        "rejections_by_reason": dict(Counter(row[3] for row in rejections)),
        "duplicate_lead_rows_merged": merged, "field_warnings": warnings,
        "events_total": event_total, "malformed_events": malformed, "duplicate_events_dropped": duplicates,
        "orphan_events": orphans, "unknown_disposition_events": unknown,
    }, set(events), samples)
