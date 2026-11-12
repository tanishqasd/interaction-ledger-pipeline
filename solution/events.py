"""Append the first copy of each identified event and its safe lookup fields."""

import io
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .normalization import normalize_phone
from .privacy import Privacy

IST = timezone(timedelta(hours=5, minutes=30))
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MICROSECOND = timedelta(microseconds=1)
_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):?[0-5][0-9])?"
)
_EVENT_TYPES = {
    "call.initiated", "call.telephony_failed", "call.connected", "call.ended",
    "disposition.set", "disposition.override",
}


class EventInputError(ValueError):
    pass


@dataclass
class EventStats:
    total: int = 0
    malformed: int = 0
    duplicates: int = 0


def parse_timestamp(value: str) -> int:
    """Return exact UTC epoch microseconds; a timestamp without an offset is IST."""
    if not _TIMESTAMP.fullmatch(value):
        raise ValueError("Expected an ISO timestamp with seconds.")
    instant = datetime.fromisoformat(value)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=IST)
    # Also reject extreme instants that can't be represented as an IST report timestamp.
    instant = instant.astimezone(timezone.utc)
    instant.astimezone(IST)
    return (instant - _EPOCH) // _MICROSECOND


def timestamp_in_ist(utc_microseconds: int) -> datetime:
    return (_EPOCH + timedelta(microseconds=utc_microseconds)).astimezone(IST)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Not a JSON constant: {value}")


def _identifier(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and all(
        not 0xD800 <= ord(char) <= 0xDFFF for char in value
    )


def _project_event(
    event: dict, catalogue: set[tuple[str, str]], privacy: Privacy,
) -> tuple[str, str, str, int, bytes | None, str | None]:
    workflow_id, call_id, kind = (event.get(key) for key in ("workflow_id", "call_id", "event_type"))
    for label, value in (("workflow_id", workflow_id), ("call_id", call_id), ("event_type", kind)):
        if not _identifier(value):
            raise EventInputError(f"missing or invalid {label}")

    timestamp = event.get("occurred_at")
    try:
        if not isinstance(timestamp, str):
            raise ValueError
        occurred_at = parse_timestamp(timestamp)
    except (ValueError, OverflowError):
        raise EventInputError("invalid occurred_at timestamp") from None

    raw_phone = event.get("phone")
    phone = normalize_phone(raw_phone) if kind != "disposition.override" and isinstance(raw_phone, str) else None
    token = privacy.phone_token(workflow_id, phone) if phone is not None else None

    code = None
    if kind.startswith("disposition."):
        payload = event.get("payload")
        candidate = payload.get("disposition_code") if isinstance(payload, dict) else None
        if isinstance(candidate, str) and (workflow_id, candidate) in catalogue:
            code = candidate
    # Arbitrary event type text belongs in the encrypted original, not the projection.
    if kind not in _EVENT_TYPES:
        kind = "disposition.other" if kind.startswith("disposition.") else "other"
    return workflow_id, call_id, kind, occurred_at, token, code


def ingest_event_file(
    connection: sqlite3.Connection, privacy: Privacy, drop_id: int, filename: str,
    content: bytes, catalogue: set[tuple[str, str]], stats: EventStats,
) -> None:
    for line_number, raw_line in enumerate(io.BytesIO(content), start=1):
        body = raw_line.rstrip(b"\r\n")
        if line_number == 1:
            body = body.removeprefix(b"\xef\xbb\xbf")
        if not body.strip():
            continue
        stats.total += 1
        try:
            event = json.loads(body.decode("utf-8"), parse_constant=_reject_json_constant)
        except (ValueError, RecursionError):
            stats.malformed += 1
            continue
        event_id = event.get("event_id") if isinstance(event, dict) else None
        if not _identifier(event_id):
            stats.malformed += 1
            continue
        if connection.execute("SELECT 1 FROM events WHERE event_id = ?", (event_id,)).fetchone():
            stats.duplicates += 1
            continue
        try:
            projection = _project_event(event, catalogue, privacy)
        except EventInputError as error:
            raise EventInputError(f"{filename}:{line_number}: {error}.") from None
        connection.execute(
            """INSERT INTO events (event_id, drop_id, workflow_id, call_id, event_type,
               occurred_at_utc, phone_token, disposition_code, event_ciphertext)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (event_id, drop_id, *projection, privacy.encrypt_event(event_id, body)),
        )
