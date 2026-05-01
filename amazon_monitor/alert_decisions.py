"""Pure alert decision logic for search monitoring (documented, testable).

Each function returns whether to emit an alert and a machine-readable skip reason
when not emitting. Reasons are logged by StateEngine; they are not WhatsApp copy.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal


AlertType = Literal["new_product", "back_in_stock", "price_drop"]


@dataclass(frozen=True)
class AlertDecision:
    """Result of evaluating one alert rule."""

    emit: bool
    alert_type: AlertType | None
    """Set when emit is True."""

    skip_reason: str | None
    """Set when emit is False; stable token for logs (e.g. SKIP_COOLDOWN)."""


def decide_new_product(is_first_observation: bool) -> AlertDecision:
    """Whether to emit `new_product` for this search hit.

    Preconditions: candidate passed filters and has a valid ASIN.

    Args:
        is_first_observation: True if there is no existing row for this ASIN in `products`.

    Emits `new_product` only on first observation (insert path).
    """
    if is_first_observation:
        return AlertDecision(emit=True, alert_type="new_product", skip_reason=None)
    return AlertDecision(emit=False, alert_type=None, skip_reason="SKIP_NOT_NEW_ASIN")


def decide_back_in_stock(old_stock: int, new_stock: int) -> AlertDecision:
    """Whether to emit `back_in_stock`.

    Preconditions: row already existed before this cycle; stock flags are 0/1.
    New stock is presence-derived: if the ASIN appears in a healthy filtered run,
    it is treated as in-stock.

    Emits only on transition out-of-stock (0) to in-stock (1), not on first sight
    of an ASIN (that is `new_product`).
    """
    if old_stock == 0 and new_stock == 1:
        return AlertDecision(emit=True, alert_type="back_in_stock", skip_reason=None)
    if old_stock == 1 and new_stock == 1:
        return AlertDecision(emit=False, alert_type=None, skip_reason="SKIP_STILL_IN_STOCK")
    if old_stock == 0 and new_stock == 0:
        return AlertDecision(emit=False, alert_type=None, skip_reason="SKIP_STILL_OUT_OF_STOCK")
    if old_stock == 1 and new_stock == 0:
        return AlertDecision(emit=False, alert_type=None, skip_reason="SKIP_NOW_OUT_OF_STOCK")
    return AlertDecision(emit=False, alert_type=None, skip_reason="SKIP_STOCK_UNCHANGED_OTHER")


def decide_price_drop(
    old_price: float | None,
    new_price: float | None,
    last_price_alert: datetime | None,
    now: datetime,
    threshold_pct: float,
    cooldown: timedelta,
) -> AlertDecision:
    """Whether to emit `price_drop`.

    Preconditions: row already existed; prices are from the same scrape pipeline.

    Rules:
    - Both old and new price must be known (non-None) and old_price > 0.
    - new_price must be strictly below old_price.
    - Percent drop must be >= threshold_pct.
    - No alert if last_price_alert is within cooldown of `now`.
    """
    if old_price is None or new_price is None:
        return AlertDecision(emit=False, alert_type=None, skip_reason="SKIP_MISSING_PRICE")
    if old_price <= 0:
        return AlertDecision(emit=False, alert_type=None, skip_reason="SKIP_INVALID_OLD_PRICE")
    if new_price >= old_price:
        return AlertDecision(emit=False, alert_type=None, skip_reason="SKIP_PRICE_NOT_DOWN")
    pct_drop = ((old_price - new_price) / old_price) * 100
    if pct_drop < threshold_pct:
        return AlertDecision(emit=False, alert_type=None, skip_reason="SKIP_BELOW_THRESHOLD")
    if last_price_alert is not None and now - last_price_alert <= cooldown:
        return AlertDecision(emit=False, alert_type=None, skip_reason="SKIP_COOLDOWN")
    return AlertDecision(emit=True, alert_type="price_drop", skip_reason=None)
