"""Tests for the run-level report deduplication in main.deduplicate_emails.

Run from the project root:  py -3.12 -m unittest tests.test_dedup -v

Deduplication rule (runs after parsing): an email is a duplicate only if BOTH
its store and its parsed report_date match an already-seen email. Emails arrive
newest-first, so the first occurrence of a (store, report_date) pair wins.
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
    def test_same_store_different_days_are_both_kept(self):
        """Two days' reports from one store must both survive."""
        emails = [
            {"message_id": "B", "store_code": "OWND02", "report_date": "2026-06-05"},
            {"message_id": "A", "store_code": "OWND02", "report_date": "2026-06-04"},
        ]
        unique, duplicate_ids = main.deduplicate_emails(emails)

        self.assertEqual([e["message_id"] for e in unique], ["B", "A"])
        self.assertEqual(duplicate_ids, [])

    def test_late_report_sent_same_day_as_today_is_kept(self):
        """The 2026-10-01 Chatswood bug: yesterday's report sent late, on the same
        send day as today's, must not be dropped as a duplicate."""
        emails = [
            {"message_id": "OCT01", "store_code": "OWND04", "send_date": "2026-10-02",
             "report_date": "2026-10-01"},
            {"message_id": "OCT02", "store_code": "OWND04", "send_date": "2026-10-02",
             "report_date": "2026-10-02"},
        ]
        unique, duplicate_ids = main.deduplicate_emails(emails)

        self.assertEqual([e["message_id"] for e in unique], ["OCT01", "OCT02"])
        self.assertEqual(duplicate_ids, [])

    def test_true_duplicate_same_store_same_day_is_dropped(self):
        """A genuine resend (same store AND same report day) keeps the first."""
        emails = [
            {"message_id": "NEW", "store_code": "OWND01", "report_date": "2026-06-05"},
            {"message_id": "OLD", "store_code": "OWND01", "report_date": "2026-06-05"},
        ]
        unique, duplicate_ids = main.deduplicate_emails(emails)

        self.assertEqual([e["message_id"] for e in unique], ["NEW"])
        self.assertEqual(duplicate_ids, ["OLD"])

    def test_different_stores_are_independent(self):
        """Different stores never collide, even on the same day."""
        emails = [
            {"message_id": "S", "store_code": "OWND01", "report_date": "2026-06-05"},
            {"message_id": "B", "store_code": "OWND02", "report_date": "2026-06-05"},
        ]
        unique, duplicate_ids = main.deduplicate_emails(emails)

        self.assertEqual([e["message_id"] for e in unique], ["S", "B"])
        self.assertEqual(duplicate_ids, [])

    def test_unknown_store_falls_back_to_sender(self):
        """Unmapped senders are keyed by address so they don't collide with each other."""
        emails = [
            {"message_id": "X", "store_code": None, "sender_email": "x@a.com", "report_date": "2026-06-05"},
            {"message_id": "Y", "store_code": None, "sender_email": "y@a.com", "report_date": "2026-06-05"},
        ]
        unique, duplicate_ids = main.deduplicate_emails(emails)

        self.assertEqual([e["message_id"] for e in unique], ["X", "Y"])
        self.assertEqual(duplicate_ids, [])

    def test_empty_input(self):
        self.assertEqual(main.deduplicate_emails([]), ([], []))


class TestValidateParsed(unittest.TestCase):
    def test_missing_banking_report_is_rejected(self):
        """The 2026-10-02 / 10-07 Chatswood case: empty date, zero transactions."""
        self.assertIsNotNone(main.validate_parsed({"report_date": "", "transactions": []}))

    def test_date_but_no_transactions_is_rejected(self):
        self.assertIsNotNone(main.validate_parsed({"report_date": "2026-10-02", "transactions": []}))

    def test_missing_keys_are_rejected(self):
        self.assertIsNotNone(main.validate_parsed({}))

    def test_day_month_swap_is_rejected(self):
        """The 2026-10-03 Sydney/Hurstville case: read as 2026-03-10."""
        self.assertIsNotNone(main.validate_parsed(
            {"report_date": "2026-03-10", "transactions": [1]}, "2026-10-03"))

    def test_future_report_date_is_rejected(self):
        self.assertIsNotNone(main.validate_parsed(
            {"report_date": "2026-10-04", "transactions": [1]}, "2026-10-03"))

    def test_late_report_within_window_passes(self):
        self.assertIsNone(main.validate_parsed(
            {"report_date": "2026-10-01", "transactions": [1]}, "2026-10-02"))

    def test_month_late_resend_passes(self):
        """The 2026-11-03 resends: Chatswood's 2 Oct report, 32 days late."""
        self.assertIsNone(main.validate_parsed(
            {"report_date": "2026-10-02", "transactions": [1]}, "2026-11-03"))

    def test_report_past_age_limit_is_rejected(self):
        self.assertIsNotNone(main.validate_parsed(
            {"report_date": "2026-09-03", "transactions": [1]}, "2026-11-03"))

    def test_good_parse_passes(self):
        self.assertIsNone(main.validate_parsed(
            {"report_date": "2026-10-05", "transactions": [{"amount_inc_tax": 10}]}))


if __name__ == "__main__":
    unittest.main()
