import json
from datetime import datetime, timedelta, timezone

from mailsweep import report
from mailsweep.store import Ledger


def record(ledger, msg_id, start, category="interview_scheduled"):
    ledger.record({"msg_id": msg_id, "thread_id": msg_id, "internal_ts": 1_700_000_000, "folder": "INBOX",
                   "from_name": "Org", "from_email": "o@x.com", "subject": "Invite", "from_me": 0,
                   "is_reply": 0, "category": category, "needs_action": 1, "action": "attend",
                   "deadline": "", "company": "X", "summary": "meeting", "classifier": "t",
                   "meeting_links": [], "excerpt": "", "truncated": 0, "run_id": 1,
                   "events": [{"method": "REQUEST", "summary": msg_id, "organizer": "o@x.com",
                               "start_utc": start, "end_utc": start, "location": ""}]})


def test_past_meetings_drop_out_of_needs_you(tmp_path):
    ledger = Ledger(tmp_path / "l.sqlite")
    now = datetime.now(timezone.utc)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    record(ledger, "past", (now - timedelta(days=2)).strftime(fmt))
    record(ledger, "future", (now + timedelta(days=2)).strftime(fmt))
    stats = {"window_from": 0, "window_to": 1, "listed": 2, "already": 0, "new": 2, "failed": 0, "failed_ids": []}
    text = report.render(ledger, 1, stats, "UTC")
    needs = text.split("## Needs you")[1].split("## Calendar")[0]
    assert "`future`" in needs and "`past`" not in needs
    assert "Calendar invitations ahead (1)" in text
