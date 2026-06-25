"""Tests for graph_reader's not-yet-processed email selection.

Run from the project root:  python -m unittest tests.test_unprocessed_filter -v

Regression for the 2026-06-24 Chatswood incident: a recipient opened the EOD
email in the shared mailbox before the scheduled run, so it was already `isRead`
when the run queried `isRead eq false` and was silently skipped. The work queue
must be defined by the absence of the EOD_Processed category, NOT the read flag.
"""

import os
import sys
import unittest
from unittest.mock import patch

# Make src/ importable (modules use flat imports like `import config`).
SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import graph_reader  # noqa: E402

_DUMMY_PDF = [{"filename": "BankingTransactionReport.pdf", "data_base64": "x"}]


def _msg(msg_id, sender, categories, is_read=False):
    return {
        "id": msg_id,
        "from": {"emailAddress": {"address": sender, "name": sender}},
        "subject": "EOD 24/6/2026",
        "sentDateTime": "2026-06-24T05:58:00Z",
        "receivedDateTime": "2026-06-24T05:58:00Z",
        "body": {"content": ""},
        "isRead": is_read,
        "categories": categories,
    }


class TestFetchUnprocessedEmails(unittest.TestCase):
    @patch("graph_reader._fetch_pdf_attachments", return_value=_DUMMY_PDF)
    @patch("graph_reader._list_messages")
    def test_processed_category_is_excluded(self, mock_list, _mock_att):
        mock_list.return_value = [
            _msg("UNPROC", "chatswood@owndays.com.au", categories=[]),
            _msg("DONE", "sydney@owndays.com.au", categories=["EOD_Processed"]),
        ]
        emails = graph_reader.fetch_unprocessed_emails("token")
        self.assertEqual([e["message_id"] for e in emails], ["UNPROC"])

    @patch("graph_reader._fetch_pdf_attachments", return_value=_DUMMY_PDF)
    @patch("graph_reader._list_messages")
    def test_read_but_unprocessed_is_still_picked_up(self, mock_list, _mock_att):
        """The Chatswood 6/24 case: read by a human, but never processed."""
        mock_list.return_value = [
            _msg("READ_UNPROC", "chatswood@owndays.com.au", categories=[], is_read=True),
        ]
        emails = graph_reader.fetch_unprocessed_emails("token")
        self.assertEqual([e["message_id"] for e in emails], ["READ_UNPROC"])

    @patch("graph_reader._fetch_pdf_attachments", return_value=_DUMMY_PDF)
    @patch("graph_reader._list_messages")
    def test_query_does_not_filter_on_read_flag(self, mock_list, _mock_att):
        mock_list.return_value = []
        graph_reader.fetch_unprocessed_emails("token")
        odata_filter = mock_list.call_args[0][1]
        self.assertNotIn("isRead", odata_filter)

    @patch("graph_reader._fetch_pdf_attachments", return_value=_DUMMY_PDF)
    @patch("graph_reader._list_messages")
    def test_non_store_sender_is_excluded(self, mock_list, _mock_att):
        """Non-store attachment mail (e.g. the M365 migration notice) must not enter the
        pipeline now that the category — not the read flag — gates processing."""
        mock_list.return_value = [
            _msg("NOISE", "yyeoh@bluebellgroup.com", categories=[]),
            _msg("REAL", "sydney@owndays.com.au", categories=[]),
        ]
        emails = graph_reader.fetch_unprocessed_emails("token")
        self.assertEqual([e["message_id"] for e in emails], ["REAL"])


if __name__ == "__main__":
    unittest.main()
