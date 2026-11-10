from mailsweep.store import Ledger


def row(msg_id, thread, ts, from_me=0, needs_action=0, category="other"):
    return {"msg_id": msg_id, "thread_id": thread, "internal_ts": ts, "folder": "INBOX",
            "from_name": "", "from_email": "", "subject": msg_id, "from_me": from_me,
            "is_reply": 0, "category": category, "needs_action": needs_action, "action": "",
            "deadline": "", "company": "", "summary": "", "classifier": "test",
            "meeting_links": [], "events": [], "excerpt": "", "truncated": 0, "run_id": 1}


def test_item_stays_open_until_i_reply(tmp_path):
    ledger = Ledger(tmp_path / "l.sqlite")
    ledger.record(row("ask", "t1", 100, needs_action=1, category="interview_invite"))
    assert [r["msg_id"] for r in ledger.open_items()] == ["ask"]
    ledger.record(row("my-reply", "t1", 200, from_me=1, category="my_outgoing"))
    assert ledger.open_items() == []


def test_follow_up_after_my_reply_reopens(tmp_path):
    ledger = Ledger(tmp_path / "l.sqlite")
    ledger.record(row("ask", "t1", 100, needs_action=1))
    ledger.record(row("my-reply", "t1", 200, from_me=1))
    ledger.record(row("ask-again", "t1", 300, needs_action=1))
    assert [r["msg_id"] for r in ledger.open_items()] == ["ask-again"]


def test_reminder_supersedes_original_request(tmp_path):
    ledger = Ledger(tmp_path / "l.sqlite")
    ledger.record(row("ask", "t1", 100, needs_action=1))
    ledger.record(row("reminder", "t1", 300, needs_action=1))
    assert [r["msg_id"] for r in ledger.open_items()] == ["reminder"]


def test_done_closes_an_item(tmp_path):
    ledger = Ledger(tmp_path / "l.sqlite")
    ledger.record(row("ask", "t1", 100, needs_action=1))
    assert ledger.mark_handled("ask", "booked on Calendly")
    assert ledger.open_items() == []


def test_known_ids_and_failures(tmp_path):
    ledger = Ledger(tmp_path / "l.sqlite")
    ledger.record(row("a", "t", 1))
    assert ledger.known_ids(["a", "b"]) == {"a"}
    ledger.record_failure("b", "HttpError 500")
    assert ledger.pending_failures() == ["b"]
    ledger.record(row("b", "t", 2))
    assert ledger.pending_failures() == []


def test_awaiting_them(tmp_path):
    ledger = Ledger(tmp_path / "l.sqlite")
    ledger.record(row("their", "t1", 100))
    ledger.record(row("mine", "t1", 200, from_me=1))
    assert [r["msg_id"] for r in ledger.awaiting_them(older_than_ts=250, newer_than_ts=0)] == ["mine"]
    ledger.record(row("their2", "t1", 300))
    assert ledger.awaiting_them(older_than_ts=400, newer_than_ts=0) == []


def test_get_and_apply_verdict(tmp_path):
    ledger = Ledger(tmp_path / "l.sqlite")
    ledger.record(row("u", "t", 1, needs_action=1, category="unclassified"))
    assert [r["msg_id"] for r in ledger.unclassified()] == ["u"]
    assert ledger.apply_verdict("u", {"category": "rejection", "needs_action": 0, "classifier": "agent:x"})
    assert ledger.unclassified() == []
    assert ledger.get("u")["category"] == "rejection"
    assert ledger.get("missing") is None


def test_scrub_private_blanks_existing_rows(tmp_path):
    ledger = Ledger(tmp_path / "l.sqlite")
    r = row("p", "t", 1, needs_action=1)
    r.update(from_email="office@clinic.example.org", subject="Your appointment", excerpt="details", summary="s")
    ledger.record(r)
    ledger.record(row("ok", "t2", 2))
    assert ledger.scrub_private([r"clinic\.example\.org"]) == 1
    got = ledger.get("p")
    assert got["category"] == "private" and got["subject"] == "(private)"
    assert got["excerpt"] == "" and got["needs_action"] == 0
    assert ledger.get("ok")["category"] == "other"
    assert ledger.scrub_private([r"clinic\.example\.org"]) == 0
