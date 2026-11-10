"""Turn a raw Gmail message into the fields the sweep records.

Pure functions only (no network) so they can be unit-tested.
"""
from __future__ import annotations

import base64
import html
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from zoneinfo import ZoneInfo

# Windows time zone names that Outlook/Teams invites use instead of IANA ids.
WINDOWS_TZ = {
    "Eastern Standard Time": "America/New_York",
    "Central Standard Time": "America/Chicago",
    "Mountain Standard Time": "America/Denver",
    "Pacific Standard Time": "America/Los_Angeles",
    "GMT Standard Time": "Europe/London",
    "W. Europe Standard Time": "Europe/Berlin",
    "China Standard Time": "Asia/Shanghai",
    "Singapore Standard Time": "Asia/Singapore",
    "India Standard Time": "Asia/Kolkata",
    "Tokyo Standard Time": "Asia/Tokyo",
    "UTC": "UTC",
}

QUOTE_MARKERS = [
    re.compile(r"^On .{10,200}wrote:\s*$", re.M | re.S),
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}", re.M | re.I),
    re.compile(r"^From: .+\nSent: .+", re.M),
    re.compile(r"^在.{5,80}写道[:：]\s*$", re.M),
]
MEETING_LINK = re.compile(
    r"https://(?:meet\.google\.com/[a-z\-]+|[\w.\-]*zoom\.us/[jw]/\d+[^\s>\"')]*"
    r"|teams\.microsoft\.com/[^\s>\"')]+|teams\.live\.com/[^\s>\"')]+"
    r"|[\w.\-]*webex\.com/[^\s>\"')]+)",
    re.I,
)
URLDEFENSE = re.compile(r"https://urldefense\.com/v3/__(.+?)__;[^\s>\"')]*")


@dataclass
class CalendarEvent:
    method: str = ""          # REQUEST / CANCEL / REPLY
    summary: str = ""
    organizer: str = ""
    start_utc: str = ""       # ISO 8601, UTC
    end_utc: str = ""
    location: str = ""


@dataclass
class ParsedMessage:
    msg_id: str
    thread_id: str
    internal_ts: int                      # epoch seconds
    from_name: str
    from_email: str
    to: str
    subject: str
    labels: list[str]
    folder: str                           # SENT / SPAM / TRASH / INBOX / ARCHIVE
    body: str                             # new text only, quoted history removed
    full_body: str
    is_reply: bool
    from_me: bool
    meeting_links: list[str] = field(default_factory=list)
    events: list[CalendarEvent] = field(default_factory=list)
    attachment_names: list[str] = field(default_factory=list)


def _b64(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "ignore")


def html_to_text(markup: str) -> str:
    markup = re.sub(r"(?is)<(style|script|head)\b.*?</\1>", " ", markup)
    markup = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>", "\n", markup)
    text = html.unescape(re.sub(r"<[^>]+>", " ", markup))
    text = re.sub(r"[ \t ​‌͏]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


CSS_RULE = re.compile(
    r"(?:^|(?<=[\s}]))[^{}\n]{1,200}\{\s*(?:[\w-]+\s*:\s*[^;{}]+;?\s*)+\}", re.M)


def strip_noise(text: str) -> str:
    """Drop mail-gateway banners and leaked stylesheets, unwrap rewritten links.

    Some senders put their stylesheet in the text part. Left in, it eats the
    excerpt and pushes the actual request out of what the classifier sees."""
    for _ in range(3):  # rules can be adjacent; a few passes clear them all
        text, count = CSS_RULE.subn(" ", text)
        if not count:
            break
    text = re.sub(r"ZjQcmQRYFpfptBannerStart.*?ZjQcmQRYFpfptBannerEnd", " ", text, flags=re.S)
    text = re.sub(r"#pfptBanner\w+\s*\{.*?\}\s*(?:[.#\w:> ,\-]+\{.*?\}\s*)*", " ", text, flags=re.S)

    def unwrap(match: re.Match) -> str:
        return match.group(1).replace("*", "%").replace("%2F", "/").replace("%3A", ":")

    return URLDEFENSE.sub(unwrap, text)


def strip_quoted(text: str) -> str:
    """Keep only what this message adds; cut the quoted thread below it."""
    cut = len(text)
    for marker in QUOTE_MARKERS:
        found = marker.search(text)
        if found:
            cut = min(cut, found.start())
    kept = [line for line in text[:cut].splitlines() if not line.lstrip().startswith(">")]
    return "\n".join(kept).strip()


def _unfold(ics: str) -> list[str]:
    return re.sub(r"\r?\n[ \t]", "", ics).splitlines()


def _ics_time(value: str, tzid: str) -> str:
    value = value.strip()
    is_utc = value.endswith("Z")
    stamp = value.rstrip("Z")
    try:
        if "T" in stamp:
            moment = datetime.strptime(stamp, "%Y%m%dT%H%M%S")
        else:
            moment = datetime.strptime(stamp, "%Y%m%d")
    except ValueError:
        return ""
    if is_utc:
        moment = moment.replace(tzinfo=timezone.utc)
    else:
        zone_name = WINDOWS_TZ.get(tzid, tzid) or "UTC"
        try:
            moment = moment.replace(tzinfo=ZoneInfo(zone_name))
        except Exception:
            moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ics(ics: str) -> list[CalendarEvent]:
    """Extract VEVENTs. VTIMEZONE blocks also contain DTSTART lines (year
    1601 in Outlook invites); only lines inside VEVENT count."""
    events: list[CalendarEvent] = []
    method = ""
    current: CalendarEvent | None = None
    for line in _unfold(ics):
        if line.startswith("METHOD:"):
            method = line.split(":", 1)[1].strip()
        elif line.startswith("BEGIN:VEVENT"):
            current = CalendarEvent(method=method)
        elif line.startswith("END:VEVENT") and current:
            events.append(current)
            current = None
        elif current is not None and ":" in line:
            head, value = line.split(":", 1)
            name, _, params = head.partition(";")
            tzid = ""
            found = re.search(r"TZID=([^;:]+)", params)
            if found:
                tzid = found.group(1).strip('"')
            if name == "DTSTART":
                current.start_utc = _ics_time(value, tzid)
            elif name == "DTEND":
                current.end_utc = _ics_time(value, tzid)
            elif name == "SUMMARY":
                current.summary = value.strip()
            elif name == "LOCATION":
                current.location = value.strip()
            elif name == "ORGANIZER":
                current.organizer = value.replace("mailto:", "").strip()
    for event in events:
        event.method = event.method or method
    return events


def folder_of(labels: list[str]) -> str:
    for name in ("SPAM", "TRASH", "SENT", "DRAFT", "INBOX"):
        if name in labels:
            return name
    return "ARCHIVE"


def parse_message(raw: dict, me: set[str], fetch_attachment=None) -> ParsedMessage:
    """`fetch_attachment(attachment_id) -> bytes` is called only for calendar
    parts that are stored as attachments (how Outlook/Teams send invites)."""
    from .gmail import iter_parts  # local import keeps this module network-free

    headers = {h["name"].lower(): h["value"] for h in raw["payload"].get("headers", [])}
    from_name, from_email = parseaddr(headers.get("from", ""))
    plain: list[str] = []
    rich: list[str] = []
    events: list[CalendarEvent] = []
    attachments: list[str] = []
    for part in iter_parts(raw["payload"]):
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        name = part.get("filename") or ""
        if name:
            attachments.append(name)
        is_calendar = "calendar" in mime or name.lower().endswith(".ics")
        if is_calendar:
            data = body.get("data")
            text = _b64(data) if data else ""
            if not text and body.get("attachmentId") and fetch_attachment:
                text = fetch_attachment(body["attachmentId"]).decode("utf-8", "ignore")
            if text:
                events.extend(parse_ics(text))
        elif body.get("data") and not name:
            if mime.startswith("text/plain"):
                plain.append(_b64(body["data"]))
            elif mime.startswith("text/html"):
                rich.append(html_to_text(_b64(body["data"])))
    full = strip_noise("\n".join(plain) if plain else "\n".join(rich))
    if not full.strip():
        full = raw.get("snippet", "")
    labels = raw.get("labelIds", [])
    subject = headers.get("subject", "")
    return ParsedMessage(
        msg_id=raw["id"],
        thread_id=raw["threadId"],
        internal_ts=int(raw["internalDate"]) // 1000,
        from_name=from_name,
        from_email=from_email.lower(),
        to=headers.get("to", ""),
        subject=subject,
        labels=labels,
        folder=folder_of(labels),
        body=strip_quoted(full),
        full_body=full,
        is_reply=bool(headers.get("in-reply-to")) or subject.lower().startswith("re:"),
        from_me=from_email.lower() in me or "SENT" in labels,
        meeting_links=sorted(set(MEETING_LINK.findall(full))),
        events=events,
        attachment_names=attachments,
    )


def local_time(utc_iso: str, zone: str) -> str:
    if not utc_iso:
        return ""
    moment = datetime.strptime(utc_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return moment.astimezone(ZoneInfo(zone)).strftime("%Y-%m-%d %H:%M")


def is_upcoming(utc_iso: str, now: datetime | None = None, grace_hours: int = 2) -> bool:
    if not utc_iso:
        return False
    now = now or datetime.now(timezone.utc)
    moment = datetime.strptime(utc_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return moment >= now - timedelta(hours=grace_hours)
