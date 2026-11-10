from mailsweep import classify
from mailsweep.parse import CalendarEvent, ParsedMessage


def msg(**over):
    base = dict(msg_id="m1", thread_id="t1", internal_ts=1, from_name="", from_email="",
                to="", subject="", labels=[], folder="INBOX", body="", full_body="",
                is_reply=False, from_me=False)
    base.update(over)
    return ParsedMessage(**base)


def test_job_alert_from_automated_sender_is_noise():
    verdict = classify.by_rule(msg(from_email="jobalerts-noreply@linkedin.com",
                                   subject="New jobs for you: Interview Coordinator"))
    assert verdict and not verdict.needs_action


def test_ats_no_reply_with_interview_wording_is_never_noise():
    # Same kind of address as an acknowledgement, but it is an invitation.
    verdict = classify.by_rule(msg(from_email="no-reply@ashbyhq.com",
                                   subject="Next steps with Example Co",
                                   body="Please book a time for an intro call."))
    assert verdict is None


def test_verification_code_with_interview_word_goes_to_model():
    verdict = classify.by_rule(msg(from_email="no-reply@ats.example", subject="Security code",
                                   body="Use this code to schedule your interview."))
    assert verdict is None


def test_person_is_never_classified_by_rule():
    verdict = classify.by_rule(msg(from_name="Pat Lee", from_email="pat@startup.io",
                                   subject="Weekly digest of our chat"))
    assert verdict is None


def test_replies_and_calendar_mail_always_go_to_model():
    assert classify.by_rule(msg(from_email="noreply@x.com", subject="newsletter", is_reply=True)) is None
    invite = msg(from_email="noreply@x.com", subject="newsletter",
                 events=[CalendarEvent(method="REQUEST", start_utc="2030-01-01T00:00:00Z")])
    assert classify.by_rule(invite) is None


def test_my_own_mail_is_recorded_not_flagged():
    verdict = classify.by_rule(msg(from_me=True, subject="Re: availability"))
    assert verdict.category == "my_outgoing" and not verdict.needs_action


def test_no_backend_flags_everything_for_the_owner():
    verdicts, _ = classify.by_model([msg(subject="Hello")], None, "English")
    assert verdicts["m1"].needs_action and verdicts["m1"].category == "unclassified"


class Broken(classify.Backend):
    name = "broken"

    def classify(self, system, user):
        raise RuntimeError("boom")


class Partial(classify.Backend):
    name = "partial"

    def classify(self, system, user):
        return {"results": [{"id": "a", "category": "rejection", "needs_action": False,
                             "action": "", "deadline": "", "company": "X", "summary": "no"}]}


def test_backend_failure_degrades_to_attention_not_silence():
    verdicts, _ = classify.by_model([msg(msg_id="a"), msg(msg_id="b")], Broken(), "English")
    assert set(verdicts) == {"a", "b"}
    assert all(v.needs_action for v in verdicts.values())


def test_missing_entries_are_flagged():
    verdicts, _ = classify.by_model([msg(msg_id="a"), msg(msg_id="b")], Partial(), "English")
    assert verdicts["a"].category == "rejection"
    assert verdicts["b"].category == "unclassified" and verdicts["b"].needs_action


def test_calendar_invite_overrides_a_harmless_verdict():
    invite = msg(events=[CalendarEvent(method="REQUEST", start_utc="2030-01-01T00:00:00Z")])
    verdict = classify.apply_hard_signals(invite, classify.Verdict("notification", False))
    assert verdict.needs_action and verdict.category == "interview_scheduled"
    assert verdict.deadline == "2030-01-01T00:00:00Z"


def test_private_patterns():
    assert classify.is_private(msg(from_email="office@clinic.example.org"), [r"clinic\.example\.org"])
    assert not classify.is_private(msg(from_email="hr@company.com"), [r"clinic\.example\.org"])


def test_calendar_invite_without_any_model_is_still_described():
    invite = msg(subject="Sync", events=[CalendarEvent(method="REQUEST", summary="Intro call",
                                                       organizer="pat@x.com", start_utc="2030-01-01T00:00:00Z")])
    verdict = classify.apply_hard_signals(invite, classify.fallback_verdict(invite, "no model backend"))
    assert verdict.category == "interview_scheduled" and verdict.classifier == "calendar"
    assert "Intro call" in verdict.summary and "pat@x.com" in verdict.summary
