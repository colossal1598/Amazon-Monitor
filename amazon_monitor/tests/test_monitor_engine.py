import unittest
from collections import deque

from monitor_engine import (
    MonitorEngine,
    RingScheduler,
    _SweepMeterView,
    _compute_fast_retry,
    _degraded_burst_reached,
    _mass_flip_tripped,
    _should_check_aod,
)


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


class TestRingSchedulerHotPreemption(unittest.TestCase):
    def test_hot_asin_preempts_more_overdue_normals_under_saturation(self) -> None:
        # Saturated ring: every ASIN is overdue at pop time. A fast-retried ASIN
        # gets a short 15s override, making it the LEAST overdue -> a plain
        # most-overdue pop would make it wait a full ring pass. Preemption must
        # hand it back first once its 15s has elapsed.
        sched = RingScheduler(55.0)
        sched.next_due = {"A": 100.0, "B": 90.0, "C": 80.0}
        # A completes with a fast-retry override; B and C stay far more overdue.
        sched.pop_due(1000.0)  # pops C (most overdue)
        sched.complete("C", 1000.0, interval_override=15.0)
        self.assertIn("C", sched.hot)
        # 15s later the whole ring is still overdue, but C (hot) must win.
        self.assertEqual(sched.pop_due(1015.0), "C")

    def test_hot_asin_not_yet_due_falls_back_to_normal(self) -> None:
        sched = RingScheduler(55.0)
        sched.next_due = {"A": 100.0, "B": 90.0}
        sched.pop_due(1000.0)  # pops B
        sched.complete("B", 1000.0, interval_override=15.0)
        # Before B's 15s elapses, the normal most-overdue (A) is served.
        self.assertEqual(sched.pop_due(1010.0), "A")

    def test_complete_without_override_clears_hot(self) -> None:
        sched = RingScheduler(55.0)
        sched.next_due = {"A": 100.0}
        sched.pop_due(1000.0)
        sched.complete("A", 1000.0, interval_override=15.0)
        self.assertIn("A", sched.hot)
        sched.pop_due(1015.0)
        sched.complete("A", 1015.0, interval_override=None)
        self.assertNotIn("A", sched.hot)

    def test_watch_list_removal_clears_hot(self) -> None:
        sched = RingScheduler(55.0)
        sched.update_watch_list(["A", "B"], 1000.0)
        sched.pop_due(2000.0)  # pops A (earliest staggered due)
        sched.complete("A", 2000.0, interval_override=15.0)
        self.assertIn("A", sched.hot)
        sched.update_watch_list(["B"], 3000.0)
        self.assertNotIn("A", sched.hot)
        self.assertNotIn("A", sched.next_due)

    def test_hot_preemption_picks_most_overdue_among_hot(self) -> None:
        sched = RingScheduler(55.0)
        sched.next_due = {"A": 100.0, "B": 200.0}
        sched.hot = {"A", "B"}
        # Both hot and due: the more-overdue hot ASIN wins.
        self.assertEqual(sched.pop_due(1000.0), "A")


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

    def test_degraded_page_triggers_override_without_confidence(self) -> None:
        # A degraded_page skip has no stock confidence/reason, but must still fast-retry.
        override, count = _compute_fast_retry(
            confidence="", reason="", prior_count=0, max_retries=3, retry_seconds=15.0, degraded=True
        )
        self.assertEqual((override, count), (15.0, 1))

    def test_degraded_page_respects_max_retries(self) -> None:
        override, count = _compute_fast_retry(
            confidence="", reason="", prior_count=3, max_retries=3, retry_seconds=15.0, degraded=True
        )
        self.assertEqual((override, count), (None, 0))


class TestDegradedBurstReached(unittest.TestCase):
    def test_four_within_window_reaches_threshold(self) -> None:
        events: deque[float] = deque()
        results = [
            _degraded_burst_reached(events, t, window_seconds=180.0, threshold=4)
            for t in (1000.0, 1030.0, 1060.0, 1090.0)
        ]
        self.assertEqual(results, [False, False, False, True])

    def test_three_within_window_does_not_reach(self) -> None:
        events: deque[float] = deque()
        results = [
            _degraded_burst_reached(events, t, window_seconds=180.0, threshold=4)
            for t in (1000.0, 1030.0, 1060.0)
        ]
        self.assertEqual(results, [False, False, False])

    def test_old_timestamps_expire_out_of_window(self) -> None:
        events: deque[float] = deque()
        # Three events, then a long gap: the first three age out and only the recent
        # burst counts, so the threshold is never reached.
        for t in (1000.0, 1030.0, 1060.0):
            _degraded_burst_reached(events, t, window_seconds=180.0, threshold=4)
        # 400s later: the earlier three are outside the 180s window.
        reached = _degraded_burst_reached(events, 1460.0, window_seconds=180.0, threshold=4)
        self.assertFalse(reached)
        self.assertEqual(list(events), [1460.0])


class TestRegisterDegradedPage(unittest.TestCase):
    """MonitorEngine._register_degraded_page: fast-retry override + burst-driven recycle flag."""

    @staticmethod
    def _engine(config: dict | None = None) -> MonitorEngine:
        eng = MonitorEngine.__new__(MonitorEngine)
        eng.config = config or {}
        eng._fast_retry_counts = {}
        eng._pending_overrides = {}
        eng._degraded_events = deque()
        eng._breaker_degraded_events = deque()
        eng._recycle_requested = False
        return eng

    def test_degraded_skip_sets_fast_retry_override(self) -> None:
        eng = self._engine({"pdp_unknown_fast_retry_seconds": 15})
        eng._register_degraded_page("B0AAAA0001")
        self.assertEqual(eng._pending_overrides["B0AAAA0001"], 15.0)
        self.assertEqual(eng._fast_retry_counts["B0AAAA0001"], 1)

    def test_burst_of_four_requests_recycle(self) -> None:
        eng = self._engine(
            {"degraded_recycle_threshold": 4, "degraded_recycle_window_seconds": 180}
        )
        for _ in range(3):
            eng._register_degraded_page("B0AAAA0001")
        self.assertFalse(eng._recycle_requested)
        eng._register_degraded_page("B0AAAA0001")
        self.assertTrue(eng._recycle_requested)


class TestShouldCheckAod(unittest.TestCase):
    """F1 gating: previously-in-stock ASINs always re-check AOD; OOS ASINs throttle by interval."""

    def test_prior_in_stock_always_checks(self) -> None:
        # Even with a just-now last-AOD timestamp, a previously in-stock ASIN re-checks.
        self.assertTrue(
            _should_check_aod(
                prior_in_stock=True, now=1000.0, last_aod=999.0, min_interval_seconds=240.0
            )
        )

    def test_oos_checks_once_interval_elapsed(self) -> None:
        self.assertTrue(
            _should_check_aod(
                prior_in_stock=False, now=1300.0, last_aod=1000.0, min_interval_seconds=240.0
            )
        )

    def test_oos_within_interval_does_not_check(self) -> None:
        self.assertFalse(
            _should_check_aod(
                prior_in_stock=False, now=1100.0, last_aod=1000.0, min_interval_seconds=240.0
            )
        )

    def test_oos_first_ever_check_allowed(self) -> None:
        # last_aod defaults to 0.0; a large now trivially clears the interval.
        self.assertTrue(
            _should_check_aod(
                prior_in_stock=False, now=5000.0, last_aod=0.0, min_interval_seconds=240.0
            )
        )


class TestAodFailureBackoff(unittest.TestCase):
    """F1 backoff (_account_aod_outcome): a failed side-fetch records the per-ASIN timestamp
    (so it throttles instead of hot-looping) and counts toward the engine-wide disable; any
    successful fetch resets the counter."""

    @staticmethod
    def _engine(config: dict | None = None) -> MonitorEngine:
        eng = MonitorEngine.__new__(MonitorEngine)
        eng.config = config or {}
        eng._aod_last_checked = {}
        eng._aod_consecutive_failures = 0
        eng._aod_disabled_until = 0.0
        return eng

    @staticmethod
    def _row(outcome: str) -> dict:
        return {"asin": "B0AAAA0001", "aod_checked": True, "aod_outcome": outcome}

    def test_fetch_failed_records_timestamp_and_counts(self) -> None:
        eng = self._engine()
        eng._account_aod_outcome("B0AAAA0001", self._row("fetch_failed"), 1000.0)
        self.assertEqual(eng._aod_last_checked["B0AAAA0001"], 1000.0)
        self.assertEqual(eng._aod_consecutive_failures, 1)
        self.assertEqual(eng._aod_disabled_until, 0.0)

    def test_threshold_disables_aod_for_window(self) -> None:
        eng = self._engine({"aod_fail_disable_threshold": 5, "aod_fail_disable_minutes": 30})
        for _ in range(4):
            eng._account_aod_outcome("B0AAAA0001", self._row("fetch_failed"), 1000.0)
        self.assertEqual(eng._aod_disabled_until, 0.0)  # not yet at threshold
        eng._account_aod_outcome("B0AAAA0001", self._row("fetch_failed"), 1000.0)  # 5th
        self.assertEqual(eng._aod_consecutive_failures, 5)
        self.assertEqual(eng._aod_disabled_until, 1000.0 + 30 * 60.0)

    def test_success_resets_counter(self) -> None:
        eng = self._engine()
        for _ in range(3):
            eng._account_aod_outcome("B0AAAA0001", self._row("fetch_failed"), 1000.0)
        self.assertEqual(eng._aod_consecutive_failures, 3)
        eng._account_aod_outcome("B0AAAA0001", self._row("no_allowed_offer"), 1001.0)
        self.assertEqual(eng._aod_consecutive_failures, 0)

    def test_row_not_aod_checked_is_noop(self) -> None:
        eng = self._engine()
        eng._account_aod_outcome("B0AAAA0001", {"asin": "B0AAAA0001"}, 1000.0)
        self.assertNotIn("B0AAAA0001", eng._aod_last_checked)
        self.assertEqual(eng._aod_consecutive_failures, 0)


class TestMassFlipTripped(unittest.TestCase):
    """F2 circuit-breaker core: 2 distinct flips + degraded context within window -> trip."""

    def test_two_distinct_flips_with_degraded_context_trips(self) -> None:
        events: deque[tuple[float, str]] = deque()
        first = _mass_flip_tripped(
            events, 1000.0, "A", window_seconds=120.0, min_flips=2, degraded_context=True
        )
        second = _mass_flip_tripped(
            events, 1030.0, "B", window_seconds=120.0, min_flips=2, degraded_context=True
        )
        self.assertFalse(first)  # one distinct ASIN so far
        self.assertTrue(second)  # two distinct ASINs within the window

    def test_single_asin_repeated_does_not_trip(self) -> None:
        events: deque[tuple[float, str]] = deque()
        results = [
            _mass_flip_tripped(
                events, t, "A", window_seconds=120.0, min_flips=2, degraded_context=True
            )
            for t in (1000.0, 1030.0, 1060.0)
        ]
        # Same ASIN flapping is a single-ASIN sellout, never a mass flip.
        self.assertEqual(results, [False, False, False])

    def test_two_distinct_flips_without_degraded_context_does_not_trip(self) -> None:
        events: deque[tuple[float, str]] = deque()
        _mass_flip_tripped(
            events, 1000.0, "A", window_seconds=120.0, min_flips=2, degraded_context=False
        )
        tripped = _mass_flip_tripped(
            events, 1030.0, "B", window_seconds=120.0, min_flips=2, degraded_context=False
        )
        self.assertFalse(tripped)

    def test_flip_expiry_out_of_window(self) -> None:
        events: deque[tuple[float, str]] = deque()
        _mass_flip_tripped(
            events, 1000.0, "A", window_seconds=120.0, min_flips=2, degraded_context=True
        )
        # 200s later B flips: A has aged out, so only one distinct ASIN remains.
        tripped = _mass_flip_tripped(
            events, 1200.0, "B", window_seconds=120.0, min_flips=2, degraded_context=True
        )
        self.assertFalse(tripped)
        self.assertEqual(list(events), [(1200.0, "B")])


class TestMassFlipBreakerEngaged(unittest.TestCase):
    """MonitorEngine._mass_flip_breaker_engaged: trip -> hold + recycle; holds subsequent flips."""

    @staticmethod
    def _engine(config: dict | None = None) -> MonitorEngine:
        eng = MonitorEngine.__new__(MonitorEngine)
        eng.config = config or {}
        eng._flip_events = deque()
        eng._breaker_degraded_events = deque()
        eng._breaker_until = 0.0
        eng._recycle_requested = False
        eng._aes_fail_streak = 0
        eng._cycle_id = 0
        eng.telemetry = None  # unused when cycle_id == 0 path is guarded
        return eng

    def test_two_flips_under_degraded_trips_and_requests_recycle(self) -> None:
        eng = self._engine({"mass_flip_min_flips": 2, "mass_flip_window_seconds": 120})
        eng._breaker_degraded_events.append(1000.0)  # a degraded_page skip in-window
        self.assertFalse(eng._mass_flip_breaker_engaged("A", 1000.0))
        engaged = eng._mass_flip_breaker_engaged("B", 1010.0)
        self.assertTrue(engaged)
        self.assertTrue(eng._recycle_requested)
        self.assertGreater(eng._breaker_until, 1010.0)

    def test_aes_fail_streak_supplies_degraded_context(self) -> None:
        eng = self._engine({"mass_flip_min_flips": 2, "mass_flip_window_seconds": 120})
        eng._aes_fail_streak = 1  # AES soft-fail counts as degraded context
        eng._mass_flip_breaker_engaged("A", 1000.0)
        self.assertTrue(eng._mass_flip_breaker_engaged("B", 1010.0))

    def test_no_degraded_context_does_not_engage(self) -> None:
        eng = self._engine({"mass_flip_min_flips": 2, "mass_flip_window_seconds": 120})
        eng._mass_flip_breaker_engaged("A", 1000.0)
        self.assertFalse(eng._mass_flip_breaker_engaged("B", 1010.0))
        self.assertFalse(eng._recycle_requested)

    def test_active_breaker_holds_further_flips_until_expiry(self) -> None:
        eng = self._engine({"mass_flip_min_flips": 2, "mass_flip_window_seconds": 120})
        eng._breaker_degraded_events.append(1000.0)
        eng._mass_flip_breaker_engaged("A", 1000.0)
        eng._mass_flip_breaker_engaged("B", 1010.0)  # trips, hold until 1010+120=1130
        # A third ASIN mid-hold is still held.
        self.assertTrue(eng._mass_flip_breaker_engaged("C", 1100.0))
        # After the hold expires with no fresh degraded context, a lone flip is not held.
        eng._breaker_degraded_events.clear()
        eng._aes_fail_streak = 0
        self.assertFalse(eng._mass_flip_breaker_engaged("D", 2000.0))


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
