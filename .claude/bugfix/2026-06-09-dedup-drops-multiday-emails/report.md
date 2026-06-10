## Root Cause

The non-backfill deduplication in `main()` keyed its `seen` set on `sender_email`
alone (`src/main.py`, former line 155-156). When a store had more than one unread
email in a single run — e.g. two different days' EOD reports — every email after
the first was misclassified as a duplicate, marked read (and labelled
`EOD_Processed`), and dropped before processing. The data was silently lost and,
because the email was now read, it could not re-enter the unread retry queue.

This is exactly what dropped the **Burwood (OWND02) 4 Jun 2026** report during the
5 Jun run (which had two unread Burwood emails: `EOD 04/06/2026` and
`EOD - 05/06/2026`).

## Fix Description

Extracted the dedup decision into a pure function
`deduplicate_emails(emails) -> (unique_emails, duplicate_ids)` and changed the key
from `sender_email` to `(sender_email, send_date)` — matching the logic the
backfill branch already used correctly. An email is now treated as a duplicate
only when both its sender and its report day match an earlier email; two distinct
days' reports from one store are both kept.

The mark-as-read side effects, newest-first "first occurrence wins" ordering, and
log output were preserved (the per-duplicate log line now also includes the date).
A module-level `logger` was added to `src/main.py` (consistent with the other
modules) so the extracted function can log.

The backfill branch — which previously had its own near-identical inline
`(sender, send_date)` dedup — was unified to call the same `deduplicate_emails()`
function. Backfill ignores `duplicate_ids` (it never marks Gmail as read) and just
logs how many duplicates were skipped, preserving its prior behavior. The
per-duplicate log message in `deduplicate_emails()` was made action-neutral
("keeping first occurrence") since the two callers take different actions.

## Tests Added

`tests/test_dedup.py` (stdlib `unittest`, no new dependency):
- `test_same_sender_different_days_are_both_kept` — the reported bug; fails before
  the fix, passes after.
- `test_true_duplicate_same_sender_same_day_is_dropped` — genuine resends are still
  deduped.
- `test_different_senders_are_independent` — different stores never collide.
- `test_empty_input` — boundary.

Run: `python -m unittest tests.test_dedup -v` → 4 passed.

## Residual Risks

- A true same-store/same-day resend with *different content* (e.g. a corrected
  report sent the same day) still keeps only the first and drops the later one as a
  duplicate. This matches the pre-existing intended behavior and the backfill
  branch; out of scope for this bug.
- `send_date` derives from the email `Date` header (sender timezone), not the
  report's internal date. Two emails for the same trading day sent on different
  calendar days would not be deduped — but they are correctly processed as
  separate, so no data is lost.
- (Resolved) The backfill branch now shares `deduplicate_emails()`, so the two
  paths can no longer drift apart.

## Git Commits

Not yet committed — awaiting review. Changed files:
- `src/main.py` (add `deduplicate_emails()`, module-level logger, rewire branch)
- `tests/test_dedup.py`, `tests/__init__.py` (new)
- `.claude/bugfix/2026-06-09-dedup-drops-multiday-emails/` (repro.md, plan.md, report.md)
