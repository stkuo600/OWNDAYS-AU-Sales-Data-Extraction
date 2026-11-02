"""Tests for claude_parser.parse_report_period (deterministic D/M/YYYY conversion).

Run from the project root:  py -3.12 -m unittest tests.test_report_period -v

The model copies the Banking Transaction Report Period verbatim; the date is
converted in code so day and month can never be swapped (2026-10-03 was once
read as 2026-03-10).
"""

import os
import sys
import unittest

# Make src/ importable (modules use flat imports like `import config`).
SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from claude_parser import parse_report_period  # noqa: E402


class TestParseReportPeriod(unittest.TestCase):
    def test_ambiguous_day_month_is_read_as_au(self):
        """The 2026-10-03 incident: 3/10/2026 is 3 October, not 10 March."""
        self.assertEqual(parse_report_period("3/10/2026 to 3/10/2026"), "2026-10-03")

    def test_zero_padded(self):
        self.assertEqual(parse_report_period("03/10/2026 to 03/10/2026"), "2026-10-03")

    def test_day_above_twelve(self):
        self.assertEqual(parse_report_period("29/10/2026 to 29/10/2026"), "2026-10-29")

    def test_single_date(self):
        self.assertEqual(parse_report_period("Period 1/10/2026"), "2026-10-01")

    def test_multi_day_period_is_rejected(self):
        """One JSON per store per day — a range must not be filed under one day."""
        self.assertEqual(parse_report_period("1/10/2026 to 3/10/2026"), "")

    def test_us_order_invalid_date_is_rejected(self):
        """10/29/2026 has no month 29 in D/M/YYYY, so it is rejected."""
        self.assertEqual(parse_report_period("10/29/2026 to 10/29/2026"), "")

    def test_iso_or_empty_is_rejected(self):
        """The model must copy the Period, not convert it; ISO output is a miss."""
        self.assertEqual(parse_report_period("2026-10-03"), "")
        self.assertEqual(parse_report_period(""), "")
        self.assertEqual(parse_report_period(None), "")


if __name__ == "__main__":
    unittest.main()
