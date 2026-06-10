## Root Cause

The non-backfill deduplication in `main()` (`src/main.py:149-172`) keys its `seen`
set on `sender_email` only. When a store has more than one unread email in a run
(e.g. two different days' reports), every email after the first is misclassified
as a duplicate, marked read, and excluded from processing — silently losing that
day's sales data. The backfill branch already keys correctly on
`(sender_email, send_date)`.

## Proposed Fix

1. Extract the dedup decision into a pure, testable function:
   `deduplicate_emails(emails) -> (unique_emails, duplicate_ids)`, keyed on
   `(sender_email, send_date)`. First occurrence of each pair wins (preserves the
   existing newest-first semantics). Logging of each duplicate stays inside the
   function to preserve current log output.
2. Replace the inline non-backfill block so it calls `deduplicate_emails()` and
   then marks `duplicate_ids` as read (unchanged side-effect logic).

The marking-as-read loop, ordering, and log messages are preserved. The only
behavioral change is the dedup key now includes `send_date`.

## Files to Modify

- `src/main.py` — add `deduplicate_emails()`, rewire the non-backfill branch.

## Test Strategy

`tests/test_dedup.py` (stdlib `unittest`, no new dependency):
- same sender / different days → both kept, no duplicates (the reported bug)
- same sender / same day → first kept, rest reported as duplicate
- different senders / same day → both kept
- empty input → empty result

Failing before the fix (function absent), passing after.
