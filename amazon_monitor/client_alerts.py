"""Rate-limited client WhatsApp alerts for operational failures."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from telemetry_store import TelemetryStore, _parse_il, israel_now_iso
from webhook_sender import send_client_message

LOGGER = logging.getLogger(__name__)

PDP_SCRAPE_ERROR_REASONS = frozenset({"navigation_failed", "parse_failed", "goto_failed"})

CLIENT_MESSAGES: dict[str, str] = {
    "captcha": "המערכת נתקלה באימות אבטחה. הסריקה מושהית זמנית ותחודש אוטומטית.",
    "cycle_failed": "מחזור הסריקה הסתיים בשגיאה. המערכת תנסה שוב במועד הבא.",
    "network_blocked": "זוהתה בעיית תקשורת. הסריקה הושהתה עד לשיחזור החיבור.",
    "stalled": "מחזור הסריקה חרג ממועד הריענון המתוכנן.",
    "chronically_stalled": "הסריקה איטית באופן ממושך. מומלץ לבדוק את המערכת.",
    "scrape_degraded": "חלק ניכר מהדפים לא נסרק בהצלחה. ייתכנו פערים בעדכונים.",
    "recovery": "הסריקה חודשה והמערכת פועלת כרגיל.",
    "health_check_failed": "בדיקת התקינות נכשלה: יתכן שהמעקב תקוע או שהתהליך לא רץ. כדאי לבדוק את המערכת.",
}


def _enabled(config: dict[str, Any]) -> bool:
    return bool(config.get("client_alerts_enabled", True))


def _recipient(config: dict[str, Any]) -> str | None:
    to = str(config.get("wa_client_to") or "").strip()
    return to or None


def count_pdp_scrape_errors(pdp_rows: list[dict[str, Any]]) -> tuple[int, dict[str, int]]:
    reasons: dict[str, int] = {}
    total = 0
    for row in pdp_rows:
        if not isinstance(row, dict) or not row.get("_skip_update"):
            continue
        reason = str(row.get("skip_reason") or "")
        if reason not in PDP_SCRAPE_ERROR_REASONS:
            continue
        total += 1
        reasons[reason] = reasons.get(reason, 0) + 1
    return total, reasons


def _cooldown_minutes(config: dict[str, Any], kind: str) -> int:
    if kind == "chronically_stalled":
        return int(config.get("client_alert_chronic_stall_cooldown_hours", 24)) * 60
    return int(config.get("client_alert_cooldown_minutes", 30))


def _max_per_window(config: dict[str, Any]) -> int:
    return int(config.get("client_alert_max_per_window", 3))


def _window_hours(config: dict[str, Any]) -> int:
    return int(config.get("client_alert_window_hours", 6))


def maybe_alert(
    kind: str,
    config: dict[str, Any],
    telemetry: TelemetryStore | None,
    *,
    cycle_id: int | None = None,
    detail: str | None = None,
) -> bool:
    """Send a client alert if rate limits allow. Returns True if sent."""
    if not _enabled(config) or not _recipient(config):
        return False
    if kind not in CLIENT_MESSAGES:
        return False

    now = israel_now_iso()
    state = telemetry.get_alert_state(kind) if telemetry else None
    cooldown = _cooldown_minutes(config, kind)

    if state:
        try:
            if _parse_il(now) - _parse_il(state["last_sent_at_il"]) < timedelta(minutes=cooldown):
                LOGGER.info("client_alert_suppressed kind=%s reason=cooldown", kind)
                if telemetry and cycle_id and detail:
                    telemetry.debug(cycle_id, "client_alert_detail", kind=kind, detail=detail, suppressed="cooldown")
                return False
        except ValueError:
            pass

        try:
            window_start = _parse_il(state["window_started_il"])
            if _parse_il(now) - window_start <= timedelta(hours=_window_hours(config)):
                if state["sent_count_in_window"] >= _max_per_window(config):
                    LOGGER.info("client_alert_suppressed kind=%s reason=max_per_window", kind)
                    if telemetry and cycle_id and detail:
                        telemetry.debug(
                            cycle_id, "client_alert_detail", kind=kind, detail=detail, suppressed="max_per_window"
                        )
                    return False
        except ValueError:
            pass

    message = CLIENT_MESSAGES[kind]
    send_client_message(message, config)
    if telemetry:
        telemetry.record_alert_sent(kind)
        if cycle_id and detail:
            telemetry.debug(cycle_id, "client_alert_detail", kind=kind, detail=detail)
    return True


def reset_stall_tracker(telemetry: TelemetryStore) -> None:
    tracker = telemetry.get_stall_tracker()
    tracker["consecutive_stall_count"] = 0
    tracker["suppressed_stall_count"] = 0
    tracker["last_timely_cycle_il"] = israel_now_iso()
    telemetry.set_stall_tracker(tracker)


def on_cycle_timing(
    summary: dict[str, Any],
    config: dict[str, Any],
    telemetry: TelemetryStore | None,
    *,
    cycle_id: int | None = None,
) -> None:
    if not telemetry:
        return
    if summary.get("exceeds_poll_interval"):
        check_stalled(summary, config, telemetry, cycle_id=cycle_id)
    else:
        reset_stall_tracker(telemetry)
        maybe_recovery(config, telemetry)


def check_stalled(
    summary: dict[str, Any],
    config: dict[str, Any],
    telemetry: TelemetryStore | None,
    *,
    cycle_id: int | None = None,
) -> None:
    if not telemetry or not summary.get("exceeds_poll_interval"):
        return

    tracker = telemetry.get_stall_tracker()
    tracker["consecutive_stall_count"] = int(tracker.get("consecutive_stall_count") or 0) + 1
    telemetry.set_stall_tracker(tracker)

    # A single cycle running a little over the poll interval (e.g. one AES/SERP hiccup
    # that already recovered within the same cycle) is not worth paging about — only
    # alert once it happens on back-to-back cycles.
    min_consecutive = max(1, int(config.get("client_alert_stall_min_consecutive", 2)))
    if tracker["consecutive_stall_count"] < min_consecutive:
        check_chronically_stalled(config, telemetry, cycle_id=cycle_id)
        return

    sent = maybe_alert(
        "stalled",
        config,
        telemetry,
        cycle_id=cycle_id,
        detail=f"total_sec={summary.get('total_sec')} consecutive={tracker['consecutive_stall_count']}",
    )
    if not sent:
        tracker = telemetry.get_stall_tracker()
        tracker["suppressed_stall_count"] = int(tracker.get("suppressed_stall_count") or 0) + 1
        telemetry.set_stall_tracker(tracker)

    check_chronically_stalled(config, telemetry, cycle_id=cycle_id)


def check_chronically_stalled(
    config: dict[str, Any],
    telemetry: TelemetryStore | None,
    *,
    cycle_id: int | None = None,
) -> None:
    if not telemetry:
        return
    tracker = telemetry.get_stall_tracker()
    consecutive = int(tracker.get("consecutive_stall_count") or 0)
    suppressed = int(tracker.get("suppressed_stall_count") or 0)
    ratio = telemetry.recent_stall_ratio(10)
    threshold = int(config.get("client_alert_chronic_stall_consecutive", 5))

    fire = consecutive >= threshold or ratio >= 0.8 or (suppressed >= 2 and consecutive >= 3)
    if not fire:
        return
    maybe_alert(
        "chronically_stalled",
        config,
        telemetry,
        cycle_id=cycle_id,
        detail=f"consecutive={consecutive} ratio={ratio:.2f} suppressed={suppressed}",
    )


def check_scrape_degraded(
    pdp_rows: list[dict[str, Any]],
    aes_outcome: dict[str, Any] | None,
    watch_count: int,
    config: dict[str, Any],
    telemetry: TelemetryStore | None,
    *,
    cycle_id: int | None = None,
) -> None:
    scrape_errors, _reasons = count_pdp_scrape_errors(pdp_rows)
    min_fail = int(config.get("client_alert_scrape_fail_min", 2))
    ratio_threshold = float(config.get("client_alert_scrape_fail_ratio", 0.5))

    pdp_degraded = watch_count >= 1 and scrape_errors >= min_fail and (scrape_errors / watch_count) >= ratio_threshold

    aes_bad_this_cycle = False
    if isinstance(aes_outcome, dict):
        nav_ok = aes_outcome.get("navigation_ok")
        cards = int(aes_outcome.get("cards_found") or 0)
        total = aes_outcome.get("total_result_count")
        if nav_ok is False:
            aes_bad_this_cycle = True
        elif nav_ok and cards == 0 and total is not None and int(total) > 0:
            aes_bad_this_cycle = True

    # AES/SERP is a single lightweight page check that now fails fast and retries on its
    # own within the same cycle (see search_scraper soft-error detection). One isolated
    # bad cycle is not "a significant portion of pages failed" — only alert once AES has
    # been unhealthy for several consecutive cycles in a row.
    aes_degraded = False
    aes_consecutive = 0
    if telemetry:
        tracker = telemetry.get_aes_fail_tracker()
        aes_consecutive = int(tracker.get("consecutive_fail_count") or 0) + 1 if aes_bad_this_cycle else 0
        tracker["consecutive_fail_count"] = aes_consecutive
        telemetry.set_aes_fail_tracker(tracker)
        min_consecutive = max(1, int(config.get("client_alert_aes_fail_consecutive", 3)))
        aes_degraded = aes_bad_this_cycle and aes_consecutive >= min_consecutive
    else:
        # No telemetry available to track consecutiveness (e.g. direct/test calls) —
        # fall back to alerting immediately, matching prior behavior.
        aes_degraded = aes_bad_this_cycle

    if not pdp_degraded and not aes_degraded:
        return

    detail_parts = []
    if pdp_degraded:
        detail_parts.append(f"pdp_errors={scrape_errors}/{watch_count}")
    if aes_degraded:
        detail_parts.append(f"aes_outcome={aes_outcome} consecutive_fails={aes_consecutive}")
    maybe_alert(
        "scrape_degraded",
        config,
        telemetry,
        cycle_id=cycle_id,
        detail="; ".join(detail_parts),
    )


def maybe_recovery(config: dict[str, Any], telemetry: TelemetryStore | None) -> None:
    if not telemetry or not _enabled(config) or not _recipient(config):
        return
    tracker = telemetry.get_stall_tracker()
    if not tracker.get("pending_recovery"):
        return
    # Clear the flag either way so a later re-enable doesn't fire a stale "recovered"
    # ping for an incident that's long gone. The "all clear" follow-up message was
    # judged pure noise (every alert already implies things will retry on their own),
    # so it's off by default; flip client_alert_recovery_enabled on to restore it.
    tracker["pending_recovery"] = False
    telemetry.set_stall_tracker(tracker)
    if not bool(config.get("client_alert_recovery_enabled", False)):
        return
    send_client_message(CLIENT_MESSAGES["recovery"], config)
