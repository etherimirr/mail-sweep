#!/usr/bin/env python3
"""mail-sweep: read every message, keep a ledger, surface what needs you.

    python sweep.py auth                 one-time Gmail sign-in (read-only)
    python sweep.py run                  read everything new since the last run
    python sweep.py run --days 14        (re)check the last 14 days
    python sweep.py audit --days 30      prove the ledger covers the last 30 days
    python sweep.py done <msg_id> [note] mark an item as handled
    python sweep.py pending --json p.json   dump mail no classifier has judged yet
    python sweep.py apply v.json            write verdicts for that mail back
    python sweep.py scrub                re-apply private_patterns to mail already in the ledger
    python sweep.py status               recent runs
    python sweep.py export --since 2026-01-01T00:00:00Z --csv out.csv
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
import tomllib
from pathlib import Path

from mailsweep import classify, gmail, parse, report
from mailsweep.store import Ledger, utc_now

HERE = Path(__file__).resolve().parent
DEFAULTS = {
    "token_path": "~/.config/mail-sweep/token.json",
    "client_secret_path": "~/.config/mail-sweep/client_secret.json",
    "ledger_path": str(HERE / "data" / "ledger.sqlite"),
    "report_dir": str(HERE / "reports"),
    "timezone": "America/New_York",
    "summary_language": "English",
    "my_addresses": [],
    "private_patterns": [],
    "important_senders": [],
    "llm": "auto",
    "model": "claude-opus-5-5",
    "batch_size": 8,
    "overlap_days": 3,
    "first_run_days": 14,
    "follow_up_days": 4,
}


def load_config(path: str | None) -> dict:
    config = dict(DEFAULTS)
    candidate = Path(path).expanduser() if path else HERE / "config.toml"
    if candidate.exists():
        config.update(tomllib.loads(candidate.read_text()))
    elif path:
        raise SystemExit(f"config file not found: {candidate}")
    return config


def read_one(service, ledger: Ledger, msg_id: str, me: set[str], config: dict):
    """Fetch and parse one message. Returns (parsed, verdict-or-None)."""
    raw = gmail.get_message(service, msg_id)
    parsed = parse.parse_message(
        raw, me, fetch_attachment=lambda att: gmail.get_attachment(service, msg_id, att)
    )
    if classify.is_private(parsed, config["private_patterns"]):
        return parsed, classify.Verdict("private", False, summary="(private, not read)", classifier="private")
    return parsed, classify.by_rule(parsed)


def save(ledger: Ledger, parsed, verdict, run_id: int, truncated: bool = False, important=()) -> None:
    private = verdict.category == "private"
    verdict = verdict if private else classify.apply_hard_signals(parsed, verdict, important)
    ledger.record({
        "msg_id": parsed.msg_id,
        "thread_id": parsed.thread_id,
        "internal_ts": parsed.internal_ts,
        "folder": parsed.folder,
        "from_name": parsed.from_name,
        "from_email": parsed.from_email,
        "subject": "(private)" if private else parsed.subject,
        "from_me": int(parsed.from_me),
        "is_reply": int(parsed.is_reply),
        "category": verdict.category,
        "needs_action": int(verdict.needs_action),
        "action": verdict.action,
        "deadline": verdict.deadline,
        "company": verdict.company,
        "summary": verdict.summary,
        "classifier": verdict.classifier,
        "meeting_links": [] if private else parsed.meeting_links,
        "events": [] if private else [e.__dict__ for e in parsed.events],
        "excerpt": "" if private else parsed.body[:4000],
        "truncated": int(truncated),
        "run_id": run_id,
    })


def process(service, ledger: Ledger, ids: list[str], me: set[str], config: dict,
            backend, run_id: int) -> list[str]:
    """Read and record `ids`. Returns the ids that could not be read."""
    failed: list[str] = []
    queue: list = []

    def flush() -> None:
        if not queue:
            return
        verdicts, truncated = classify.by_model(queue, backend, config["summary_language"])
        for item in queue:
            save(ledger, item, verdicts[item.msg_id], run_id, item.msg_id in truncated,
                 config.get("important_senders", []))
        queue.clear()

    for index, msg_id in enumerate(ids, 1):
        try:
            parsed, verdict = read_one(service, ledger, msg_id, me, config)
        except Exception as err:
            ledger.record_failure(msg_id, f"{type(err).__name__}: {err}")
            failed.append(msg_id)
            continue
        if verdict is not None:
            save(ledger, parsed, verdict, run_id, important=config.get("important_senders", []))
        else:
            queue.append(parsed)
            if len(queue) >= config["batch_size"]:
                flush()
        if index % 50 == 0:
            print(f"  read {index}/{len(ids)}", file=sys.stderr, flush=True)
    flush()
    return failed


def complete_threads(service, ledger: Ledger, me: set[str], config: dict, backend, run_id: int) -> int:
    """For every open item, make sure the whole thread is in the ledger, so
    that "did I already answer this?" is decided on complete information."""
    added = 0
    for thread_id in {row["thread_id"] for row in ledger.open_items()}:
        try:
            ids = gmail.thread_message_ids(service, thread_id)
        except Exception:
            continue
        missing = [i for i in ids if i not in ledger.known_ids(ids)]
        if missing:
            process(service, ledger, missing, me, config, backend, run_id)
            added += len(missing)
    return added


def cmd_run(args, config: dict) -> int:
    ledger = Ledger(config["ledger_path"])
    service = gmail.connect(config["token_path"])
    me = {a.lower() for a in config["my_addresses"]} | {gmail.my_address(service)}
    now = int(time.time())
    if args.days:
        window_from = now - args.days * 86400
    else:
        last = ledger.last_ok_window_to()
        window_from = (last - config["overlap_days"] * 86400) if last else now - config["first_run_days"] * 86400
    backend = classify.pick_backend(args.llm or config["llm"], config["model"])
    print(f"classifier: {'rules + ' + backend.name if backend else 'rules only (unknown mail is flagged for you)'}",
          file=sys.stderr)

    run_id = ledger.start_run(window_from, now)
    listed = gmail.list_ids(service, window_from)
    retry = [i for i in ledger.pending_failures() if i not in listed]
    known = ledger.known_ids(listed)
    todo = [i for i in listed if i not in known] + retry
    print(f"listed {len(listed)}, already read {len(known)}, to read {len(todo)}", file=sys.stderr)
    process(service, ledger, todo, me, config, backend, run_id)
    complete_threads(service, ledger, me, config, backend, run_id)

    # The check that matters: every id Gmail listed must now be in the ledger.
    still_missing = [i for i in listed if i not in ledger.known_ids(listed)]
    stats = {
        "window_from": window_from, "window_to": now, "listed": len(listed),
        "already": len(known), "new": len(todo) - len(still_missing),
        "failed": len(still_missing), "failed_ids": still_missing,
    }
    ledger.finish_run(run_id, len(listed), len(known), stats["new"], len(still_missing))
    text = report.render(ledger, run_id, stats, config["timezone"], config["follow_up_days"])
    out_dir = Path(config["report_dir"]).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M")
    (out_dir / f"sweep-{stamp}.md").write_text(text)
    (out_dir / "latest.md").write_text(text)
    print(text)
    return 0 if not still_missing else 2


def cmd_audit(args, config: dict) -> int:
    """List the window again and report anything the ledger does not hold."""
    ledger = Ledger(config["ledger_path"])
    service = gmail.connect(config["token_path"])
    now = int(time.time())
    listed = gmail.list_ids(service, now - args.days * 86400)
    known = ledger.known_ids(listed)
    missing = [i for i in listed if i not in known]
    print(f"last {args.days} days: Gmail lists {len(listed)}, ledger holds {len(known)}, missing {len(missing)}")
    for msg_id in missing[:200]:
        print("  missing", msg_id)
    return 0 if not missing else 1


def cmd_done(args, config: dict) -> int:
    ledger = Ledger(config["ledger_path"])
    if not ledger.mark_handled(args.msg_id, " ".join(args.note)):
        print("no such message id in the ledger", file=sys.stderr)
        return 1
    print("marked handled")
    return 0


def cmd_pending(args, config: dict) -> int:
    """Hand-off: write out every message still unclassified, so a person or
    an agent session can judge them and feed the verdicts back with `apply`."""
    import json

    ledger = Ledger(config["ledger_path"])
    items = [{
        "id": row["msg_id"],
        "from": f"{row['from_name']} <{row['from_email']}>",
        "subject": row["subject"],
        "folder": row["folder"],
        "received_utc": time.strftime("%Y-%m-%d %H:%M", time.gmtime(row["internal_ts"])),
        "is_reply_in_thread": bool(row["is_reply"]),
        "calendar_events": json.loads(row["events"] or "[]"),
        "meeting_links": json.loads(row["meeting_links"] or "[]"),
        "text": (row["excerpt"] or "")[:args.chars],
    } for row in ledger.unclassified()]
    Path(args.json).write_text(json.dumps(items, ensure_ascii=False, indent=1))
    print(f"{len(items)} unclassified message(s) written to {args.json}")
    print("Expected reply format: " + json.dumps({"results": [{
        "id": "...", "category": "|".join(classify.CATEGORIES), "needs_action": False,
        "action": "", "deadline": "", "company": "", "summary": ""}]}))
    return 0


def cmd_apply(args, config: dict) -> int:
    import json

    ledger = Ledger(config["ledger_path"])
    data = json.loads(Path(args.json).read_text())
    applied = 0
    for item in data.get("results", []):
        category = item.get("category")
        if category not in classify.CATEGORIES:
            print(f"skipped {item.get('id')}: unknown category {category!r}", file=sys.stderr)
            continue
        needs_action = bool(item.get("needs_action"))
        row = ledger.get(str(item["id"]))
        if row is None:
            print(f"skipped {item.get('id')}: not in the ledger", file=sys.stderr)
            continue
        # A calendar invite with a start time stays surfaced whatever the verdict says.
        if any(e.get("start_utc") and e.get("method", "").upper() != "CANCEL"
               for e in json.loads(row["events"] or "[]")):
            needs_action = True
            if category in ("other", "notification", "personal"):
                category = "interview_scheduled"
        # Sign-ups (registration, resume drop/book) are surfaced whatever the verdict says.
        v = classify.Verdict(category, needs_action, action=item.get("action", ""))
        classify.apply_signup_signal(v, row["subject"] or "", row["excerpt"] or "",
                                     f"{row['from_name'] or ''} <{row['from_email'] or ''}>",
                                     config.get("important_senders", []))
        category, needs_action = v.category, v.needs_action
        applied += ledger.apply_verdict(str(item["id"]), {
            "category": category,
            "needs_action": int(needs_action),
            "action": v.action,
            "deadline": item.get("deadline", ""),
            "company": item.get("company", ""),
            "summary": item.get("summary", ""),
            "classifier": f"agent:{args.by}",
        })
    print(f"applied {applied} verdict(s); still unclassified: {len(ledger.unclassified())}")
    return 0


def cmd_report(args, config: dict) -> int:
    ledger = Ledger(config["ledger_path"])
    runs = ledger.recent_runs(1)
    if not runs:
        print("no runs yet")
        return 1
    run = runs[0]
    stats = {"window_from": run["window_from"], "window_to": run["window_to"], "listed": run["listed"] or 0,
             "already": run["already_read"] or 0, "new": run["newly_read"] or 0,
             "failed": run["failed"] or 0, "failed_ids": ledger.pending_failures()}
    text = report.render(ledger, run["run_id"], stats, config["timezone"], config["follow_up_days"])
    out_dir = Path(config["report_dir"]).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "latest.md").write_text(text)
    print(text)
    return 0


def cmd_scrub(args, config: dict) -> int:
    ledger = Ledger(config["ledger_path"])
    count = ledger.scrub_private(config["private_patterns"])
    print(f"scrubbed {count} message(s) now matching private_patterns")
    return 0


def cmd_status(args, config: dict) -> int:
    ledger = Ledger(config["ledger_path"])
    for run in ledger.recent_runs():
        print(f"run {run['run_id']}: {run['started_at']} → {run['finished_at']} "
              f"listed {run['listed']} new {run['newly_read']} failed {run['failed']} [{run['status']}]")
    print(f"open items: {len(ledger.open_items())}")
    return 0


def cmd_export(args, config: dict) -> int:
    ledger = Ledger(config["ledger_path"])
    rows = ledger.read_since(args.since)
    fields = ["msg_id", "thread_id", "internal_ts", "folder", "from_email", "subject", "category",
              "needs_action", "action", "deadline", "company", "summary", "classifier", "read_at"]
    with open(args.csv, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(fields)
        for row in rows:
            writer.writerow([row[f] for f in fields])
    print(f"wrote {len(rows)} rows to {args.csv}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", help="path to config.toml (default: ./config.toml)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("auth")
    run = sub.add_parser("run")
    run.add_argument("--days", type=int, help="re-check this many days instead of resuming")
    run.add_argument("--llm", choices=["auto", "anthropic", "claude-cli", "none"])
    audit = sub.add_parser("audit")
    audit.add_argument("--days", type=int, default=30)
    done = sub.add_parser("done")
    done.add_argument("msg_id")
    done.add_argument("note", nargs="*")
    sub.add_parser("status")
    sub.add_parser("scrub")
    sub.add_parser("report")
    pending = sub.add_parser("pending")
    pending.add_argument("--json", required=True)
    pending.add_argument("--chars", type=int, default=1500, help="characters of text per message")
    apply_ = sub.add_parser("apply")
    apply_.add_argument("json")
    apply_.add_argument("--by", default="manual", help="who produced the verdicts")
    export = sub.add_parser("export")
    export.add_argument("--since", default="1970-01-01T00:00:00Z")
    export.add_argument("--csv", required=True)
    args = parser.parse_args()
    config = load_config(args.config)
    if args.command == "auth":
        gmail.authorize(config["client_secret_path"], config["token_path"])
        return 0
    return {"run": cmd_run, "audit": cmd_audit, "done": cmd_done,
            "status": cmd_status, "export": cmd_export, "pending": cmd_pending,
            "apply": cmd_apply, "report": cmd_report, "scrub": cmd_scrub}[args.command](args, config)


if __name__ == "__main__":
    sys.exit(main())
