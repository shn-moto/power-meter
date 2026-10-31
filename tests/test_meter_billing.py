import unittest
from datetime import datetime, timezone

from app.storage import _calculate_monthly_underpayment


class MeterBillingTests(unittest.TestCase):
    def test_partial_month_below_quota_has_zero_underpayment(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = datetime(2026, 1, 6, tzinfo=timezone.utc)
        months, underpayment = _calculate_monthly_underpayment(
            [[(start, 1000.0), (end, 1100.0)]], start, end
        )
        self.assertEqual(months, 1)
        self.assertEqual(underpayment, 0.0)

    def test_partial_month_above_quota_uses_only_excess(self):
        start = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
        end = datetime(2026, 10, 28, 20, 4, tzinfo=timezone.utc)
        months, underpayment = _calculate_monthly_underpayment(
            [[(start, 0.0), (end, 303.0)]], start, end
        )
        self.assertEqual(months, 1)
        self.assertAlmostEqual(underpayment, 53.0)

    def test_five_full_months_and_early_sixth(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        timeline = [
            (datetime(2026, month, 1, tzinfo=timezone.utc), value)
            for month, value in [(1, 0.0), (2, 450.0), (3, 900.0), (4, 1350.0), (5, 1800.0), (6, 2250.0)]
        ]
        early_end = datetime(2026, 6, 6, tzinfo=timezone.utc)
        months, underpayment = _calculate_monthly_underpayment(
            [timeline + [(early_end, 2325.0)]], start, early_end
        )
        self.assertEqual(months, 6)
        self.assertEqual(underpayment, 1000.0)

        full_end = datetime(2026, 7, 1, tzinfo=timezone.utc)
        months, underpayment = _calculate_monthly_underpayment(
            [timeline + [(full_end, 2700.0)]], start, full_end
        )
        self.assertEqual(months, 6)
        self.assertEqual(underpayment, 1200.0)

    def test_owner_interval_has_six_cycles(self):
        start = datetime(2026, 4, 14, 12, tzinfo=timezone.utc)
        end = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
        months, underpayment = _calculate_monthly_underpayment(
            [[(start, 0.0), (end, 2756.0)]], start, end
        )
        self.assertEqual(months, 6)
        self.assertAlmostEqual(underpayment, 1256.0)

    def test_empty_interval_has_no_quota_or_underpayment(self):
        start = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
        months, underpayment = _calculate_monthly_underpayment(
            [[(start, 100.0)]], start, start
        )
        self.assertEqual((months, underpayment), (0, 0.0))


if __name__ == "__main__":
    unittest.main()
