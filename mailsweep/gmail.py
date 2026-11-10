"""Gmail access: read-only, with backoff, never filtering anything out.

The one rule this module enforces: `list_ids` returns EVERY message id in the
window — inbox, sent, spam, trash, all categories. Filtering is the caller's
job and happens after a message is recorded, never before.
"""
from __future__ import annotations

import base64
import random
import time
from pathlib import Path
from typing import Iterator

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
RETRY_STATUS = {403, 429, 500, 502, 503, 504}


def connect(token_path: str | Path):
    """Build a Gmail service from an authorized-user token file."""
    token_path = Path(token_path).expanduser()
    if not token_path.exists():
        raise SystemExit(
            f"No OAuth token at {token_path}. Run `python sweep.py auth` first "
            "(see README: Setup)."
        )
    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def authorize(client_secret_path: str | Path, token_path: str | Path) -> None:
    """Run the desktop OAuth flow once and save a read-only token."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(
        str(Path(client_secret_path).expanduser()), SCOPES
    )
    creds = flow.run_local_server(port=0, prompt="consent")
    token_path = Path(token_path).expanduser()
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json())
    print(f"token saved to {token_path}")


def execute(request, max_tries: int = 10):
    """Run a Gmail API request, backing off on quota and server errors.

    Gmail's per-minute quota returns 403 rateLimitExceeded, not 429, so both
    are retried. Any other error is raised: a message we could not fetch must
    surface as a failure, never be skipped silently.
    """
    delay = 2.0
    for attempt in range(max_tries):
        try:
            return request.execute()
        except HttpError as err:
            status = getattr(err.resp, "status", None)
            if status not in RETRY_STATUS or attempt == max_tries - 1:
                raise
            time.sleep(delay + random.uniform(0, 1))
            delay = min(delay * 2, 60)


def my_address(service) -> str:
    return execute(service.users().getProfile(userId="me"))["emailAddress"].lower()


def list_ids(service, after_epoch: int, before_epoch: int | None = None) -> list[str]:
    """Every message id in the window. No label, category or sender filter."""
    query = f"after:{after_epoch}"
    if before_epoch:
        query += f" before:{before_epoch}"
    ids: list[str] = []
    token = None
    while True:
        resp = execute(
            service.users().messages().list(
                userId="me",
                q=query,
                includeSpamTrash=True,
                maxResults=500,
                pageToken=token,
            )
        )
        ids.extend(m["id"] for m in resp.get("messages", []))
        token = resp.get("nextPageToken")
        if not token:
            return ids


def get_message(service, msg_id: str) -> dict:
    return execute(service.users().messages().get(userId="me", id=msg_id, format="full"))


def get_attachment(service, msg_id: str, attachment_id: str) -> bytes:
    resp = execute(
        service.users().messages().attachments().get(
            userId="me", messageId=msg_id, id=attachment_id
        )
    )
    return base64.urlsafe_b64decode(resp["data"])


def thread_message_ids(service, thread_id: str) -> list[str]:
    resp = execute(
        service.users().threads().get(userId="me", id=thread_id, format="minimal")
    )
    return [m["id"] for m in resp.get("messages", [])]


def iter_parts(payload: dict) -> Iterator[dict]:
    yield payload
    for part in payload.get("parts", []) or []:
        yield from iter_parts(part)
