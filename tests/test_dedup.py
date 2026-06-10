"""Tests for the run-level email deduplication in main.deduplicate_emails.

Run from the project root:  python -m unittest tests.test_dedup -v

Deduplication rule: an email is a duplicate only if BOTH its sender address and
its send_date match an already-seen email. Emails arrive newest-first, so the
first occurrence of a (sender, send_date) pair wins.
"""

import os
import sys
import unittest

# Make src/ importable (modules use flat imports like `import config`).
SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import main  # noqa: E402


class TestDeduplicateEmails(unittest.TestCase):
    def test_same_sender_different_days_are_both_kept(self):
        """The reported bug: two days' reports from one store must both survive.

        Newest-first ordering (Gmail) puts 5 Jun before 4 Jun.
        """
        emails = [
            {"message_id": "B", "sender_email": "owndaysburwood@gmail.com", "send_date": "2026-06-05"},
            {"message_id": "A", "sender_email": "owndaysburwood@gmail.com", "send_date": "2026-06-04"},
        ]
        unique, duplicate_ids = main.deduplicate_emails(emails)

        kept_ids = [e["message_id"] for e in unique]
        self.assertEqual(kept_ids, ["B", "A"])
        self.assertEqual(duplicate_ids, [])

    def test_true_duplicate_same_sender_same_day_is_dropped(self):
        """A genuine resend (same sender AND same day) keeps the first, drops the rest."""
        emails = [
            {"message_id": "NEW", "sender_email": "sydney@owndays.com.au", "send_date": "2026-06-05"},
            {"message_id": "OLD", "sender_email": "sydney@owndays.com.au", "send_date": "2026-06-05"},
        ]
        unique, duplicate_ids = main.deduplicate_emails(emails)

        self.assertEqual([e["message_id"] for e in unique], ["NEW"])
        self.assertEqual(duplicate_ids, ["OLD"])

    def test_different_senders_are_independent(self):
        """Different stores never collide, even on the same day."""
        emails = [
            {"message_id": "S", "sender_email": "sydney@owndays.com.au", "send_date": "2026-06-05"},
            {"message_id": "B", "sender_email": "owndaysburwood@gmail.com", "send_date": "2026-06-05"},
        ]
        unique, duplicate_ids = main.deduplicate_emails(emails)

        self.assertEqual([e["message_id"] for e in unique], ["S", "B"])
        self.assertEqual(duplicate_ids, [])

    def test_empty_input(self):
        self.assertEqual(main.deduplicate_emails([]), ([], []))


if __name__ == "__main__":
    unittest.main()
