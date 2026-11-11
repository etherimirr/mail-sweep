"""Render what a sweep found as Markdown."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .parse import is_upcoming, local_time
from .store import Ledger


def _when(epoch: int, zone: str) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).astimezone(ZoneInfo(zone)).strftime("%m-%d %H:%M")


def _who(row) -> str:
    return row["company"] or row["from_name"] or row["from_email"] or "?"


def _event_is_over(row, now: datetime) -> bool:
    """True when every calendar event attached to the message is in the past."""
    events = [e for e in json.loads(row["events"] or "[]") if e.get("start_utc")]
    return bool(events) and not any(is_upcoming(e["start_utc"], now) for e in events)


# Interviews that already took place need nobody; anything else whose date has
# passed (resume drop, info session, application window) still needs a recovery
# step, so it is listed separately instead of being dropped.
_DONE_WHEN_OVER = {"interview_scheduled", "interview_invite"}

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def deadline_passed(deadline: str, now: datetime) -> bool:
    """Best effort: True when the free-text deadline names a date before today.

    Understands 2026-11-06, 11/6(/2026) and "Nov 6"; anything else is treated
    as not passed so it stays in the main list.
    """
    text = (deadline or "").strip().lower()
    if not text:
        return False
    year = now.year
    m = re.search(r"(20\d\d)-(\d{1,2})-(\d{1,2})", text)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", text)
        if m:
            mo, d = int(m.group(1)), int(m.group(2))
            y = int(m.group(3)) if m.group(3) else year
            y = y + 2000 if y < 100 else y
        else:
            m = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?!\d)", text)
            if not m:
                return False
            mo, d, y = _MONTHS[m.group(1)], int(m.group(2)), year
    try:
        when = datetime(y, mo, d, 23, 59, tzinfo=timezone.utc)
    except ValueError:
        return False
    return when < now - timedelta(hours=12)


def _is_past(row, now: datetime) -> bool:
    return _event_is_over(row, now) or deadline_passed(row["deadline"], now)


def render(ledger: Ledger, run_id: int, stats: dict, zone: str, follow_up_days: int = 4) -> str:
    now = datetime.now(timezone.utc)
    lines: list[str] = []
    out = lines.append
    out(f"# Mail sweep {now.astimezone(ZoneInfo(zone)).strftime('%Y-%m-%d %H:%M')} ({zone})")
    out("")
    out("## Coverage")
    out(f"- Window: {_when(stats['window_from'], zone)} to {_when(stats['window_to'], zone)}")
    out(f"- Listed by Gmail: {stats['listed']} messages (inbox, sent, spam, trash, every category)")
    out(f"- Already in the ledger: {stats['already']}; read now: {stats['new']}; failed: {stats['failed']}")
    if stats["failed"]:
        out(f"- **INCOMPLETE**: {stats['failed']} message(s) could not be read and will be retried: "
            + ", ".join(stats["failed_ids"][:20]))
    else:
        out("- Every listed message is in the ledger.")
    out("")

    unjudged = ledger.unclassified()
    if unjudged:
        out(f"## Not classified yet ({len(unjudged)})")
        out("No classifier has judged these. Run `sweep.py pending --json p.json`, judge them, "
            "then `sweep.py apply`. Until then they are treated as needing you.")
        out("")
    candidates = [r for r in ledger.open_items() if r["category"] != "unclassified"]
    open_items = [r for r in candidates if not _is_past(r, now)]
    past_items = [r for r in candidates
                  if _is_past(r, now) and not (r["category"] in _DONE_WHEN_OVER and _event_is_over(r, now))]
    out(f"## Needs you ({len(open_items)})")
    if not open_items:
        out("Nothing open.")
    for row in open_items:
        _item(out, row, zone)
    out("")

    out(f"## Past deadline, still recover ({len(past_items)})")
    out("Never drop these: check whether the form still accepts submissions, apply on the company site, "
        "or write to the sender/recruiter asking to be included late.")
    for row in past_items:
        _item(out, row, zone)
    out("")

    _rest(out, ledger, run_id, now, zone, follow_up_days)
    return "\n".join(lines)


def _item(out, row, zone: str) -> None:
    flag = " `SPAM folder`" if row["folder"] == "SPAM" else ""
    out(f"- **{_who(row)}** · {row['category']} · received {_when(row['internal_ts'], zone)}{flag}")
    out(f"  - {row['summary'] or row['subject']}")
    if row["action"]:
        out(f"  - Do: {row['action']}")
    if row["deadline"]:
        out(f"  - By: {row['deadline']}")
    links = json.loads(row["meeting_links"] or "[]")
    if links:
        out(f"  - Link: {links[0]}")
    out(f"  - id `{row['msg_id']}` · subject: {row['subject']}")


def _rest(out, ledger: Ledger, run_id: int, now: datetime, zone: str, follow_up_days: int) -> None:
    upcoming = []
    for row in ledger.with_events():
        for event in json.loads(row["events"] or "[]"):
            if event.get("method", "").upper() != "CANCEL" and is_upcoming(event.get("start_utc", ""), now):
                upcoming.append((event["start_utc"], event, row))
    # The latest invite for the same summary wins (updates replace originals).
    latest: dict[str, tuple] = {}
    for start, event, row in sorted(upcoming, key=lambda item: item[2]["internal_ts"]):
        latest[event.get("summary", "") + "|" + event.get("organizer", "")] = (start, event, row)
    out(f"## Calendar invitations ahead ({len(latest)})")
    for start, event, row in sorted(latest.values(), key=lambda item: item[0]):
        end = local_time(event.get("end_utc", ""), zone)[-5:]
        out(f"- {local_time(start, zone)}–{end} · {event.get('summary', '')} · organiser {event.get('organizer', '')}")
        out(f"  - {event.get('location', '')} · invite received {_when(row['internal_ts'], zone)} · id `{row['msg_id']}`")
    out("")

    cutoff = int(now.timestamp()) - follow_up_days * 86400
    waiting = ledger.awaiting_them(cutoff, int(now.timestamp()) - 30 * 86400)
    out(f"## You wrote last, no reply for {follow_up_days}+ days ({len(waiting)})")
    for row in waiting:
        out(f"- {row['subject']} · sent {_when(row['internal_ts'], zone)} · thread `{row['thread_id']}`")
    out("")

    counts: dict[str, int] = {}
    for row in ledger.run_messages(run_id):
        counts[row["category"]] = counts.get(row["category"], 0) + 1
    out("## Read in this run, by category")
    for category, count in sorted(counts.items(), key=lambda item: -item[1]):
        out(f"- {category}: {count}")
    out("")
