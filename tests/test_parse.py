from mailsweep import parse

OUTLOOK_ICS = """BEGIN:VCALENDAR
METHOD:REQUEST
BEGIN:VTIMEZONE
TZID:Eastern Standard Time
BEGIN:STANDARD
DTSTART:16010101T020000
END:STANDARD
BEGIN:DAYLIGHT
DTSTART:16010101T020000
END:DAYLIGHT
END:VTIMEZONE
BEGIN:VEVENT
ORGANIZER;CN=Sam Example:mailto:sam@example.com
SUMMARY;LANGUAGE=en-US:Example Co: Candidate / Sam
DTSTART;TZID=Eastern Standard Time:20261110T160000
DTEND;TZID=Eastern Standard Time:20261110T163000
LOCATION;LANGUAGE=en-US:Microsoft Teams Meeting
END:VEVENT
END:VCALENDAR
"""


def test_outlook_invite_ignores_timezone_block_and_converts_to_utc():
    events = parse.parse_ics(OUTLOOK_ICS)
    assert len(events) == 1
    event = events[0]
    assert event.method == "REQUEST"
    assert event.start_utc == "2026-11-10T21:00:00Z"   # 16:00 Eastern in November
    assert event.end_utc == "2026-11-10T21:30:00Z"
    assert event.organizer == "sam@example.com"
    assert parse.local_time(event.start_utc, "America/New_York") == "2026-11-10 16:00"


def test_utc_and_folded_lines():
    ics = "BEGIN:VEVENT\r\nSUMMARY:Intro\r\n  call\r\nDTSTART:20260105T150000Z\r\nEND:VEVENT\r\n"
    event = parse.parse_ics(ics)[0]
    assert event.summary == "Intro call"
    assert event.start_utc == "2026-01-05T15:00:00Z"


def test_strip_quoted_keeps_only_new_text():
    text = "Thursday at 3 works.\n\nOn Mon, Nov 9, 2026 at 10:36 AM Pat <p@x.com> wrote:\n> Are you free?\n"
    assert parse.strip_quoted(text) == "Thursday at 3 works."


def test_html_to_text_drops_style_and_tags():
    markup = "<html><head><style>p{color:red}</style></head><body><p>Hello<br>there</p></body></html>"
    assert parse.html_to_text(markup) == "Hello\nthere"


def test_meeting_links_found():
    text = "Join https://meet.google.com/abc-defg-hij or https://us04web.zoom.us/j/123456789?pwd=x"
    links = parse.MEETING_LINK.findall(text)
    assert any("meet.google.com" in link for link in links)
    assert any("zoom.us/j/123456789" in link for link in links)


def test_urldefense_links_are_unwrapped():
    wrapped = "https://urldefense.com/v3/__https://meet.google.com/abc-defg-hij__;!!TOKEN$"
    assert parse.strip_noise(wrapped) == "https://meet.google.com/abc-defg-hij"


def test_folder_priority():
    assert parse.folder_of(["INBOX", "SPAM"]) == "SPAM"
    assert parse.folder_of(["SENT"]) == "SENT"
    assert parse.folder_of(["CATEGORY_UPDATES"]) == "ARCHIVE"
