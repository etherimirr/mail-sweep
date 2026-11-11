from datetime import datetime, timezone

from mailsweep.report import deadline_passed

NOW = datetime(2026, 11, 10, 18, 0, tzinfo=timezone.utc)


def test_iso_date_in_past():
    assert deadline_passed("2026-11-06 13:00 ET", NOW)


def test_slash_date_in_past():
    assert deadline_passed("Friday 10/30 @ 9am", NOW)


def test_month_name_in_past():
    assert deadline_passed("Wednesday, November 4th at 3PM ET", NOW)


def test_future_date_not_passed():
    assert not deadline_passed("2026-11-14 01:55", NOW)
    assert not deadline_passed("Nov 17", NOW)


def test_today_not_passed():
    assert not deadline_passed("11/10 5pm", NOW)


def test_unparseable_kept_open():
    assert not deadline_passed("ASAP", NOW)
    assert not deadline_passed("", NOW)
