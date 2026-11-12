"""Resolve the ledger against current leads and derive call facts without writes."""

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from .events import timestamp_in_ist


@dataclass(frozen=True)
class Disposition:
    code: str
    is_terminal: bool
    is_success: bool


@dataclass(frozen=True)
class Call:
    call_id: str
    initiated_at_utc: int
    connected: bool
    telephony_failed: bool
    disposition: Disposition | None

    @property
    def initiated_at_ist(self) -> datetime:
        return timestamp_in_ist(self.initiated_at_utc)


@dataclass(frozen=True)
class InteractionSnapshot:
    calls_by_lead: dict[int, list[Call]]
    orphan_events: int
    unknown_disposition_events: int


@dataclass
class _CallEvents:
    initiated_at_utc: int | None = None
    connected: bool = False
    telephony_failed: bool = False
    latest_set: Disposition | None = None
    latest_override: Disposition | None = None

    def add(self, event: sqlite3.Row) -> None:
        match event["event_type"]:
            case "call.initiated":
                if self.initiated_at_utc is None:
                    self.initiated_at_utc = event["occurred_at_utc"]
            case "call.connected":
                self.connected = True
            case "call.telephony_failed":
                self.telephony_failed = True
            case "disposition.set" | "disposition.override":
                disposition = Disposition(event["disposition_code"], bool(event["is_terminal"]),
                                          bool(event["is_success"]))
                if event["event_type"] == "disposition.override":
                    self.latest_override = disposition
                else:
                    self.latest_set = disposition


def derive_interactions(connection: sqlite3.Connection) -> InteractionSnapshot:
    # One query gives both passes a consistent view. The order fixes timestamp ties by ID.
    events = connection.execute(
        """SELECT e.workflow_id, e.call_id, e.event_type, e.occurred_at_utc,
                  e.phone_token, e.disposition_code, l.lead_id, d.is_terminal, d.is_success
           FROM events e
           LEFT JOIN leads l ON l.workflow_id = e.workflow_id AND l.phone_token = e.phone_token
           LEFT JOIN dispositions d ON d.workflow_id = e.workflow_id AND d.code = e.disposition_code
           ORDER BY e.occurred_at_utc, e.event_id"""
    ).fetchall()

    # Even an as-yet unlinked phone counts toward ambiguity. Never guess between two phones.
    call_links: dict[tuple[str, str], tuple[bytes, int | None] | None] = {}
    for event in events:
        token = event["phone_token"]
        if token is None or event["event_type"] == "disposition.override":
            continue
        key = (event["workflow_id"], event["call_id"])
        previous = call_links.setdefault(key, (token, event["lead_id"]))
        if previous is not None and previous[0] != token:
            call_links[key] = None

    grouped: dict[tuple[int, str], _CallEvents] = {}
    orphans = unknown = 0
    for event in events:
        kind = event["event_type"]
        lead_id = event["lead_id"]
        if kind == "disposition.override":
            link = call_links.get((event["workflow_id"], event["call_id"]))
            lead_id = None if link is None else link[1]
        if lead_id is None:
            orphans += 1
        elif kind.startswith("disposition.") and event["disposition_code"] is None:
            unknown += 1
        else:
            grouped.setdefault((lead_id, event["call_id"]), _CallEvents()).add(event)

    calls_by_lead: dict[int, list[Call]] = {}
    for (lead_id, call_id), facts in grouped.items():
        if facts.initiated_at_utc is not None:
            call = Call(call_id, facts.initiated_at_utc, facts.connected, facts.telephony_failed,
                        facts.latest_override or facts.latest_set)
            calls_by_lead.setdefault(lead_id, []).append(call)
    for calls in calls_by_lead.values():
        calls.sort(key=lambda call: (call.initiated_at_utc, call.call_id))
    return InteractionSnapshot(calls_by_lead, orphans, unknown)


def lead_bucket(calls: Sequence[Call]) -> str:
    if not calls:
        return "yet_to_reach_out"
    if any(call.disposition and call.disposition.is_terminal for call in calls):
        return "completed"
    if all(call.telephony_failed for call in calls):
        return "in_progress_telephony_failed"
    return "in_progress_dialed"
