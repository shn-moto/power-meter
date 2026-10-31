import unittest
from datetime import datetime, timedelta, timezone

from app.storage import _counter_bar_energies_kwh


class GapRecoveryCounterTests(unittest.TestCase):
    def setUp(self):
        self.counter_meta = ("1", 10.0)
        self.gap_start = datetime(2026, 9, 1, 19, 0, tzinfo=timezone.utc)
        self.reconnect = datetime(2026, 10, 31, 20, 0, tzinfo=timezone.utc)

    @staticmethod
    def _row(bucket, captured_at, first_counter, last_counter):
        return {
            "bucket": bucket,
            "first_raw_dps": {"1": round(first_counter * 10)},
            "last_raw_dps": {"1": round(last_counter * 10)},
            "first_captured_at": captured_at,
            "last_captured_at": captured_at + timedelta(minutes=50),
        }

    def test_reconnect_jump_is_counted_without_estimates(self):
        rows = [
            self._row(self.gap_start, self.gap_start, 396.9, 397.0),
            self._row(self.reconnect, self.reconnect, 490.8, 490.8),
        ]

        energies = _counter_bar_energies_kwh(rows, self.counter_meta)

        self.assertAlmostEqual(energies[0], 0.1)
        self.assertAlmostEqual(energies[1], 93.8)

    def test_reconnect_jump_is_suppressed_with_estimates(self):
        rows = [
            self._row(self.gap_start, self.gap_start, 396.9, 397.0),
            self._row(self.reconnect, self.reconnect, 490.8, 490.8),
        ]

        energies = _counter_bar_energies_kwh(
            rows,
            self.counter_meta,
            suppress_gap_jumps=True,
        )

        self.assertAlmostEqual(energies[0], 0.1)
        self.assertEqual(energies[1], 0.0)

    def test_online_increment_in_reconnect_bucket_is_retained(self):
        rows = [
            self._row(self.gap_start, self.gap_start, 396.9, 397.0),
            self._row(self.reconnect, self.reconnect, 490.8, 491.0),
        ]

        energies = _counter_bar_energies_kwh(
            rows,
            self.counter_meta,
            suppress_gap_jumps=True,
        )

        self.assertAlmostEqual(energies[1], 0.2)

    def test_pre_range_anchor_also_suppresses_reconnect_jump(self):
        rows = [self._row(self.reconnect, self.reconnect, 490.8, 491.0)]

        energies = _counter_bar_energies_kwh(
            rows,
            self.counter_meta,
            initial_anchor_kwh=397.0,
            initial_anchor_captured_at=self.gap_start + timedelta(minutes=50),
            suppress_gap_jumps=True,
        )

        self.assertAlmostEqual(energies[0], 0.2)


if __name__ == "__main__":
    unittest.main()
