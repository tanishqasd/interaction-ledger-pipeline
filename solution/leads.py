"""Validate one workflow's CSV and merge its accepted leads."""

import csv
import io
import json
import sqlite3
from dataclasses import dataclass

from .normalization import normalize_amount, normalize_date, normalize_header, normalize_phone
from .privacy import Privacy


class LeadInputError(ValueError):
    pass


@dataclass
class LeadStats:
    total: int = 0
    rejected: int = 0
    merged: int = 0
    warnings: int = 0


def _column_positions(header: list[str], fields: list[dict]) -> dict[str, int]:
    names = {
        normalize_header(label): field["name"]
        for field in fields for label in (field["name"], *field.get("aliases", ()))
    }
    positions: dict[str, int] = {}
    for index, label in enumerate(header):
        name = names.get(normalize_header(label))
        if name is not None:
            positions.setdefault(name, index)
    return positions


def _normalize_value(field: dict, value: str) -> str | None:
    match field["type"]:
        case "string":
            return value
        case "phone":
            return normalize_phone(value)
        case "date":
            return normalize_date(value)
        case "decimal":
            return normalize_amount(value)
        case "enum":
            return next((choice.lower() for choice in field["values"]
                         if choice.casefold() == value.casefold()), None)
        case _:
            raise LeadInputError(f"Unsupported field type for {field['name']}.")


def _validate_row(
    row: list[str], positions: dict[str, int], fields: list[dict],
) -> tuple[dict[str, str | None], str | None, int]:
    values: dict[str, str | None] = {}
    warnings = 0
    for field in fields:
        name = field["name"]
        index = positions.get(name)
        value = row[index].strip() if index is not None and index < len(row) else ""
        if not value:
            if field["required"]:
                return {}, f"missing_required:{name}", 0
            values[name] = None
            continue

        normalized = _normalize_value(field, value)
        if normalized is None:
            if field["type"] == "phone":
                return {}, "invalid_phone", 0
            if field["required"]:
                return {}, f"missing_required:{name}", 0
            warnings += 1
        values[name] = normalized
    return values, None, warnings


def _merge_lead(
    connection: sqlite3.Connection, privacy: Privacy, workflow_id: str, batch_pk: int,
    fields: list[dict], reference_name: str, incoming: dict[str, str | None],
) -> bool:
    phone = incoming["phone"]
    assert phone is not None  # Validation requires a canonical phone before merging.
    token = privacy.phone_token(workflow_id, phone)
    previous = connection.execute(
        """SELECT lead_id, external_ref, attributes_json, pii_ciphertext FROM leads
           WHERE workflow_id = ? AND phone_token = ?""", (workflow_id, token),
    ).fetchone()

    merged = {}
    if previous is not None:
        merged.update(json.loads(previous["attributes_json"]))
        merged.update(privacy.decrypt_lead(workflow_id, token, previous["pii_ciphertext"]))
        merged[reference_name] = previous["external_ref"]
    merged.update({name: value for name, value in incoming.items() if value is not None})

    # An absent value in the incoming row must never act like a defaulted update.
    public, sensitive = {}, {}
    for field in fields:
        name = field["name"]
        if name != reference_name:
            target = sensitive if field.get("pii") or name == "phone" else public
            value = merged.get(name)
            target[name] = field.get("default") if value is None else value

    record = (
        merged[reference_name], json.dumps(public, ensure_ascii=False),
        privacy.encrypt_lead(workflow_id, token, sensitive),
    )
    if previous is None:
        connection.execute(
            """INSERT INTO leads
               (workflow_id, first_batch_pk, phone_token, phone_last4, external_ref,
                attributes_json, pii_ciphertext) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (workflow_id, batch_pk, token, phone[-4:], *record),
        )
    else:
        connection.execute(
            """UPDATE leads SET external_ref = ?, attributes_json = ?, pii_ciphertext = ?
               WHERE lead_id = ?""", (*record, previous["lead_id"]),
        )
    return previous is not None


def ingest_batch(
    connection: sqlite3.Connection, privacy: Privacy, drop_id: int, workflow_id: str,
    batch_id: str, fields: list[dict], content: bytes, stats: LeadStats,
) -> None:
    references = [field for field in fields if field.get("reference")]
    if len(references) != 1 or references[0].get("pii") or references[0]["name"] == "phone":
        raise LeadInputError(f"Workflow {workflow_id} must have one non-PII external reference.")
    reference_name = references[0]["name"]
    batch_pk = connection.execute(
        "INSERT INTO batches (drop_id, workflow_id, batch_id) VALUES (?, ?, ?)",
        (drop_id, workflow_id, batch_id),
    ).lastrowid

    with io.TextIOWrapper(io.BytesIO(content), encoding="utf-8-sig", newline="") as file:
        reader = csv.reader(file, strict=True)
        try:
            positions = _column_positions(next(reader, []), fields)
            row_number = 0
            for row in reader:
                if not any(cell.strip() for cell in row):
                    continue
                row_number += 1
                stats.total += 1
                incoming, reason, warnings = _validate_row(row, positions, fields)
                if reason is not None:
                    connection.execute(
                        "INSERT INTO rejections (batch_pk, row_number, reason) VALUES (?, ?, ?)",
                        (batch_pk, row_number, reason),
                    )
                    stats.rejected += 1
                    continue
                stats.warnings += warnings
                if _merge_lead(connection, privacy, workflow_id, batch_pk, fields, reference_name, incoming):
                    stats.merged += 1
        except (csv.Error, UnicodeDecodeError):
            raise LeadInputError(f"Unreadable CSV: {workflow_id}/{batch_id}.csv near line {reader.line_num + 1}.") from None
