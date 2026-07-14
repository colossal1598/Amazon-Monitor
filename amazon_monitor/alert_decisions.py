"""Pure alert decision logic for PDP monitoring (documented, testable).

Each function returns whether to emit an alert and a machine-readable skip reason
when not emitting. Reasons are logged by StateEngine; they are not WhatsApp copy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


AlertType = Literal["new_product", "back_in_stock", "price_drop", "price_below_target"]


@dataclass(frozen=True)
class AlertDecision:
    """Result of evaluating one alert rule."""

    emit: bool
    alert_type: AlertType | None
    """Set when emit is True."""

    skip_reason: str | None
    """Set when emit is False; stable token for logs (e.g. SKIP_COOLDOWN)."""


# Decide if we should send a “new product” alert by only allowing it the first time we ever see that ASIN.
def decide_new_product(is_first_observation: bool) -> AlertDecision:
    """Whether to emit `new_product` for this PDP observation.

    Preconditions: candidate passed filters and has a valid ASIN.

    Args:
        is_first_observation: True if there is no existing row for this ASIN in `products`.

    Emits `new_product` only on first observation (insert path).
    """
    if is_first_observation:
        return AlertDecision(emit=True, alert_type="new_product", skip_reason=None)
    return AlertDecision(emit=False, alert_type=None, skip_reason="SKIP_NOT_NEW_ASIN")


# Decide if we should send a “back in stock” alert by checking if the item went from missing to present again.
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


# Decide if we should send a “price drop” alert by comparing the last stored price to the new PDP price.
def decide_price_drop(
    old_price: float | None,
    new_price: float | None,
    threshold_pct: float,
) -> AlertDecision:
    """Whether to emit `price_drop`.

    Preconditions: row already existed; prices are consecutive PDP observations.

    Rules:
    - Both old and new price must be known (non-None) and old_price > 0.
    - new_price must be strictly below old_price.
    - Percent drop must be >= threshold_pct.
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
    return AlertDecision(emit=True, alert_type="price_drop", skip_reason=None)


# Decide if we should send a "price below target" alert: qualifying seller, known price
# strictly under the configured target, and the per-ASIN hysteresis latch still armed.
def decide_price_below_target(
    price: float | None,
    qualifies: bool,
    target: float | None,
    armed: bool,
) -> AlertDecision:
    """Whether to emit `price_below_target` (cooldown, if any, is applied by the caller).

    Preconditions: ``qualifies`` already encodes the seller_ok/shippable/price-present
    check done upstream by the PDP scraper (paid shipping does NOT affect this — only a
    hard "cannot be shipped" disqualifies, which already makes the item non-qualifying).

    Rules:
    - No target configured -> never emit.
    - Item must currently qualify (in-stock, allowed seller) with a known price.
    - Price must be strictly below the target (item price alone, not price+shipping).
    - The hysteresis latch (``armed``) must be set; callers disarm after firing and
      re-arm once price is back at/above target or the item stops qualifying.
    """
    if target is None:
        return AlertDecision(emit=False, alert_type=None, skip_reason="SKIP_NO_TARGET")
    if not qualifies or price is None:
        return AlertDecision(emit=False, alert_type=None, skip_reason="SKIP_NOT_QUALIFYING")
    if price >= target:
        return AlertDecision(emit=False, alert_type=None, skip_reason="SKIP_PRICE_NOT_BELOW_TARGET")
    if not armed:
        return AlertDecision(emit=False, alert_type=None, skip_reason="SKIP_DISARMED")
    return AlertDecision(emit=True, alert_type="price_below_target", skip_reason=None)
