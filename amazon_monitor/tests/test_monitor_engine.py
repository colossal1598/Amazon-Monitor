import unittest

from monitor_engine import RingScheduler, _SweepMeterView, _compute_fast_retry


class TestRingSchedulerUpdateWatchList(unittest.TestCase):
    def test_new_asins_staggered_across_one_interval(self) -> None:
        sched = RingScheduler(60.0)
        now = 1000.0
        sched.update_watch_list(["B011111111", "B022222222", "B033333333"], now)

        dues = [sched.next_due[a] for a in ["B011111111", "B022222222", "B033333333"]]
        self.assertEqual(dues[0], now)
        # All due times fall within one interval and are evenly spread.
        for due in dues:
            self.assertGreaterEqual(due, now)
            self.assertLess(due, now + sched.interval)
        self.assertEqual(dues, [now, now + 20.0, now + 40.0])

    def test_existing_asins_keep_their_schedule(self) -> None:
        sched = RingScheduler(60.0)
        sched.update_watch_list(["B011111111"], 1000.0)
        original_due = sched.next_due["B011111111"]

        sched.update_watch_list(["B011111111", "B022222222"], 2000.0)
        self.assertEqual(sched.next_due["B011111111"], original_due)
        self.assertIn("B022222222", sched.next_due)

    def test_removed_asins_dropped_including_checked_out(self) -> None:
        sched = RingScheduler(60.0)
        sched.update_watch_list(["B011111111", "B022222222"], 1000.0)
        popped = sched.pop_due(5000.0)
        self.assertEqual(popped, "B011111111")
        self.assertIn("B011111111", sched.checked_out)

        sched.update_watch_list(["B022222222"], 5000.0)
        self.assertNotIn("B011111111", sched.next_due)
        self.assertNotIn("B011111111", sched.checked_out)
        self.assertIn("B022222222", sched.next_due)


class TestRingSchedulerPopDue(unittest.TestCase):
    def test_returns_most_overdue_asin(self) -> None:
        sched = RingScheduler(60.0)
        sched.next_due = {"B011111111": 500.0, "B022222222": 100.0, "B033333333": 300.0}
        self.assertEqual(sched.pop_due(1000.0), "B022222222")

    def test_returns_none_when_nothing_due(self) -> None:
        sched = RingScheduler(60.0)
        sched.next_due = {"B011111111": 2000.0}
        self.assertIsNone(sched.pop_due(1000.0))

    def test_returns_none_on_empty_scheduler(self) -> None:
        sched = RingScheduler(60.0)
        self.assertIsNone(sched.pop_due(1000.0))

    def test_never_returns_checked_out_asin(self) -> None:
        sched = RingScheduler(60.0)
        sched.next_due = {"B011111111": 100.0, "B022222222": 200.0}
        first = sched.pop_due(1000.0)
        second = sched.pop_due(1000.0)
        third = sched.pop_due(1000.0)
        self.assertEqual(first, "B011111111")
        self.assertEqual(second, "B022222222")
        self.assertIsNone(third)
        self.assertEqual(sched.checked_out, {"B011111111", "B022222222"})


class TestRingSchedulerComplete(unittest.TestCase):
    def test_complete_reschedules_and_clears_checked_out(self) -> None:
        sched = RingScheduler(60.0)
        sched.next_due = {"B011111111": 100.0}
        self.assertEqual(sched.pop_due(1000.0), "B011111111")

        sched.complete("B011111111", 1005.0)
        self.assertNotIn("B011111111", sched.checked_out)
        self.assertEqual(sched.next_due["B011111111"], 1005.0 + 60.0)
        # Becomes eligible again once the interval elapses.
        self.assertIsNone(sched.pop_due(1064.0))
        self.assertEqual(sched.pop_due(1065.0), "B011111111")

    def test_complete_for_removed_asin_only_clears_checkout(self) -> None:
        sched = RingScheduler(60.0)
        sched.checked_out.add("B011111111")
        sched.complete("B011111111", 1000.0)
        self.assertNotIn("B011111111", sched.checked_out)
        self.assertNotIn("B011111111", sched.next_due)


class TestRingSchedulerCompleteOverride(unittest.TestCase):
    def test_override_schedules_short_recheck(self) -> None:
        sched = RingScheduler(60.0)
        sched.next_due = {"B011111111": 100.0}
        sched.pop_due(1000.0)
        sched.complete("B011111111", 1000.0, interval_override=15.0)
        self.assertEqual(sched.next_due["B011111111"], 1015.0)
        # Due again after only 15s instead of the full 60s interval.
        self.assertIsNone(sched.pop_due(1014.0))
        self.assertEqual(sched.pop_due(1015.0), "B011111111")

    def test_none_override_uses_normal_interval(self) -> None:
        sched = RingScheduler(60.0)
        sched.next_due = {"B011111111": 100.0}
        sched.pop_due(1000.0)
        sched.complete("B011111111", 1000.0, interval_override=None)
        self.assertEqual(sched.next_due["B011111111"], 1060.0)

    def test_override_clamped_at_zero(self) -> None:
        sched = RingScheduler(60.0)
        sched.next_due = {"B011111111": 100.0}
        sched.complete("B011111111", 1000.0, interval_override=-5.0)
        self.assertEqual(sched.next_due["B011111111"], 1000.0)


class TestComputeFastRetry(unittest.TestCase):
    def test_no_pay_price_unknown_triggers_override(self) -> None:
        override, count = _compute_fast_retry(
            confidence="unknown", reason="no_pay_price", prior_count=0, max_retries=3, retry_seconds=15.0
        )
        self.assertEqual((override, count), (15.0, 1))

    def test_priceless_purchasable_unknown_triggers_override(self) -> None:
        override, count = _compute_fast_retry(
            confidence="unknown", reason="priceless_purchasable", prior_count=1, max_retries=3, retry_seconds=15.0
        )
        self.assertEqual((override, count), (15.0, 2))

    def test_exhausted_retries_fall_back_to_normal(self) -> None:
        override, count = _compute_fast_retry(
            confidence="unknown", reason="no_pay_price", prior_count=3, max_retries=3, retry_seconds=15.0
        )
        self.assertEqual((override, count), (None, 0))

    def test_confirmed_in_resets_and_uses_normal(self) -> None:
        override, count = _compute_fast_retry(
            confidence="confirmed_in", reason="", prior_count=2, max_retries=3, retry_seconds=15.0
        )
        self.assertEqual((override, count), (None, 0))

    def test_other_unknown_reason_does_not_fast_retry(self) -> None:
        override, count = _compute_fast_retry(
            confidence="unknown", reason="seller_mismatch", prior_count=1, max_retries=3, retry_seconds=15.0
        )
        self.assertEqual((override, count), (None, 0))


class TestRingSchedulerSecondsToNext(unittest.TestCase):
    def test_time_until_earliest_pending_due(self) -> None:
        sched = RingScheduler(60.0)
        sched.next_due = {"B011111111": 1030.0, "B022222222": 1010.0}
        self.assertEqual(sched.seconds_to_next(1000.0), 10.0)

    def test_zero_when_already_overdue(self) -> None:
        sched = RingScheduler(60.0)
        sched.next_due = {"B011111111": 900.0}
        self.assertEqual(sched.seconds_to_next(1000.0), 0.0)

    def test_small_positive_default_when_all_checked_out(self) -> None:
        sched = RingScheduler(60.0)
        sched.next_due = {"B011111111": 100.0}
        sched.pop_due(1000.0)
        wait = sched.seconds_to_next(1000.0)
        self.assertGreater(wait, 0.0)
        self.assertLessEqual(wait, 2.0)

    def test_small_positive_default_when_empty(self) -> None:
        sched = RingScheduler(60.0)
        wait = sched.seconds_to_next(1000.0)
        self.assertGreater(wait, 0.0)
        self.assertLessEqual(wait, 2.0)


class TestRingSchedulerIntervalChange(unittest.TestCase):
    def test_interval_change_affects_future_complete_rescheduling(self) -> None:
        sched = RingScheduler(60.0)
        sched.next_due = {"B011111111": 100.0}
        sched.pop_due(1000.0)
        sched.interval = 120.0
        sched.complete("B011111111", 1000.0)
        self.assertEqual(sched.next_due["B011111111"], 1000.0 + 120.0)

    def test_constructor_enforces_minimum_interval(self) -> None:
        sched = RingScheduler(1.0)
        self.assertEqual(sched.interval, 10.0)


class _FakeMeter:
    def __init__(self, totals: dict[str, int]) -> None:
        self._totals = totals

    def totals(self) -> dict[str, int]:
        return dict(self._totals)


class TestSweepMeterView(unittest.TestCase):
    def test_returns_per_key_deltas_vs_baseline(self) -> None:
        meter = _FakeMeter({"bytes_total": 500, "requests": 30})
        view = _SweepMeterView(meter, {"bytes_total": 200, "requests": 10})
        self.assertEqual(view.totals(), {"bytes_total": 300, "requests": 20})

    def test_deltas_clamped_at_zero(self) -> None:
        meter = _FakeMeter({"bytes_total": 100})
        view = _SweepMeterView(meter, {"bytes_total": 250})
        self.assertEqual(view.totals(), {"bytes_total": 0})

    def test_keys_missing_from_baseline_default_to_zero(self) -> None:
        meter = _FakeMeter({"bytes_total": 100, "new_key": 40})
        view = _SweepMeterView(meter, {"bytes_total": 60})
        self.assertEqual(view.totals(), {"bytes_total": 40, "new_key": 40})

    def test_reflects_live_meter_growth(self) -> None:
        meter = _FakeMeter({"bytes_total": 100})
        view = _SweepMeterView(meter, {"bytes_total": 100})
        self.assertEqual(view.totals(), {"bytes_total": 0})
        meter._totals["bytes_total"] = 175
        self.assertEqual(view.totals(), {"bytes_total": 75})


if __name__ == "__main__":
    unittest.main()
