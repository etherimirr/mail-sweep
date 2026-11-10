# mail-sweep

Read every message in a Gmail account, keep a ledger of what was read and when, and report what needs you: interview invitations, scheduling requests, assessments, calendar invites, and threads waiting on a reply.

It exists because a keyword-and-sender-based inbox check missed real interview invitations. The failures were all of the same kind, so the design is built around preventing them:

| What went wrong | What mail-sweep does instead |
|---|---|
| Mail was skipped by sender domain (applicant tracking systems send invitations and acknowledgements from the same no-reply address) | Nothing is filtered before it is read. Rules may label mail as noise only when the sender is automated **and** the subject matches a known template **and** the text has no action words. Mail from a person, any reply, and anything with a calendar attachment always goes to the classifier. |
| Search results were treated as "everything" | Each run lists every message id in the window (inbox, sent, spam, trash, all categories) and checks that every id is in the ledger afterwards. If one is missing the run exits non-zero and says which. |
| Only the first message of a thread was looked at | Every message is recorded individually. An item is open until a later message **from you** exists in that thread, so a reminder after your reply reopens it, and your own sent mail is read too. |
| A calendar invite attached as `.ics` was never opened | Calendar parts are parsed (including Outlook/Teams invites stored as attachments, with Windows time zone names) and any invite with a start time is surfaced regardless of what the text says. |
| There was no record of what had been read | SQLite ledger: one row per message with `read_at`, the verdict, and which classifier produced it. `audit` re-lists any period and proves coverage. |
| When classification failed, mail was quietly dropped | Any failure (no model, bad output, a missing entry) records the message as `unclassified` with `needs_action = 1`. Failure can only cause extra attention. |

## Setup

```bash
pip install -r requirements.txt
cp config.example.toml config.toml      # git-ignored; edit it
```

Gmail access is read-only (`gmail.readonly`). Create an OAuth client of type *Desktop* in Google Cloud Console, save the JSON to the `client_secret_path` in your config, then:

```bash
python sweep.py auth
```

## Use

```bash
python sweep.py run                 # everything new since the last run (plus a 3-day overlap)
python sweep.py run --days 14       # re-check the last 14 days; already-read mail is skipped
python sweep.py audit --days 30     # list the last 30 days again and report anything not in the ledger
python sweep.py done <msg_id> note  # close an item you handled outside email (e.g. booked on a scheduling page)
python sweep.py status
python sweep.py report              # re-render the report from the ledger
python sweep.py export --since 2026-01-01T00:00:00Z --csv read-log.csv
```

`run` prints a Markdown report and writes it to `reports/latest.md`:

- **Coverage**: window, how many messages Gmail listed, how many were read now, how many failed.
- **Needs you**: open items with one-line summary, what to do, deadline, meeting link and message id.
- **Calendar invitations ahead**: start time in your time zone, organiser, when the invite arrived.
- **You wrote last, no reply for N+ days**: threads to follow up.

Exit code is `0` when every listed message is in the ledger and `2` when the run is incomplete.

## Classification

Rules handle only unmistakable automated mail (job alerts, "viewed your application", verification codes, receipts). Everything else is judged by a language model:

| `llm` setting | Backend |
|---|---|
| `anthropic` | Claude through the Anthropic API (`ANTHROPIC_API_KEY`), structured JSON output |
| `claude-cli` | Claude Code in print mode (`claude -p`), for machines that are signed in but have no API key |
| `none` | No model. Unknown mail is recorded as `unclassified` and flagged |
| `auto` | The first of the above that is available |

Hand-off mode, for when no backend is available or you want an agent session to do the judging:

```bash
python sweep.py pending --json pending.json   # everything still unclassified
# ...judge them (a person, or an agent), producing {"results": [{"id": ..., "category": ..., ...}]}
python sweep.py apply verdicts.json --by my-agent
```

Message text is passed to the model as data. The prompt tells the model not to follow instructions found inside mail.

## Privacy

- Mail, tokens, the ledger, reports and `config.toml` never leave your machine through this tool and are git-ignored.
- `private_patterns` in the config marks mail (by sender or subject) that must not be read: it is counted in the ledger so coverage stays complete, but its subject and body are not stored and it is never sent to a model.
- With a model backend, the text of non-private mail is sent to that model. Use `llm = "none"` if that is not acceptable.
- Bodies longer than 6,000 characters are cut for classification; the ledger marks those rows `truncated = 1`.

## Tests

```bash
python -m pytest -q
```

The tests cover the parts where a mistake means a missed message: calendar parsing, quoted-reply stripping, the noise rules' vetoes, failure handling in classification, and the open/answered logic of the ledger.

## Layout

```
sweep.py              command line
mailsweep/gmail.py    list + fetch with backoff; no filtering
mailsweep/parse.py    bodies, quoted text, calendar invites, meeting links
mailsweep/classify.py narrow rules, model backends, fail-open fallback
mailsweep/store.py    SQLite ledger and the open-item queries
mailsweep/report.py   Markdown report
```
