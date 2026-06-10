## Symptom

When two unread EOD report emails from the *same store* (same sender address) are
present in a single processing run, only the newest one is processed. The older
day's report is logged as a "Duplicate email", marked as read (and labelled
`EOD_Processed`), and **never parsed** — so no JSON file is produced and no SFTP
upload happens. The lost report cannot re-enter the unread retry queue because it
has been marked read.

Observed in production: the **Burwood (OWND02) report for 4 Jun 2026** was dropped
this way during the 5 Jun run, which had collected two unread Burwood emails
(`EOD 04/06/2026` and `EOD - 05/06/2026`).

## Environment

- Project: OWNDAYS AU Sales Data Extraction
- File: `src/main.py`, non-backfill dedup block (lines ~149-172)
- Python 3.12, Windows
- Trigger condition: ≥2 unread emails from one sender in the same run (e.g. a
  store that didn't get processed the previous day, so two days' reports are
  unread together)

## Reproduction Steps

The faulty logic is the inline dedup in `main()`. Reproduce at the unit level with
two emails from the same sender but different `send_date`:

```python
emails = [
    {"message_id": "B", "sender_email": "owndaysburwood@gmail.com", "send_date": "2026-06-05"},
    {"message_id": "A", "sender_email": "owndaysburwood@gmail.com", "send_date": "2026-06-04"},
]
```

Gmail returns newest-first, so `B` (5 Jun) is index 0 and `A` (4 Jun) is index 1.
Current dedup keys on `sender_email` only → `A` is treated as a duplicate of `B`.

## Expected vs Actual Behavior

- **Expected:** Both emails are kept and processed — they are different days'
  reports from the same store. Only a *true* duplicate (same sender AND same
  send_date) should be dropped.
- **Actual:** `A` (4 Jun) is classified as a duplicate, its `message_id` is added
  to `duplicate_ids`, it is marked read, and it is removed from `unique_emails`
  before processing. Its sales data is silently lost.
