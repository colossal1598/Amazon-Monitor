import logging
import random
import time
from pathlib import Path

import browser_factory
from browser_factory import close_context, create_stealth_context
from exceptions import SessionExpired
from state_engine import StateEngine

LOGGER = logging.getLogger(__name__)


def run_shipping_batch(state_engine: StateEngine, batch_size: int, auth_dir: str = "auth/amazon") -> list[dict]:
    asins = state_engine.get_shipping_queue(batch_size)
    if not asins:
        return []
    Path(auth_dir).mkdir(parents=True, exist_ok=True)
    context = create_stealth_context(persistent_dir=auth_dir, headless=False)
    alerts: list[dict] = []
    try:
        page = context.new_page()
        for asin in asins:
            if browser_factory.global_rate_limiter:
                browser_factory.global_rate_limiter.acquire()
            page.goto(f"https://www.amazon.com/dp/{asin}", wait_until="domcontentloaded", timeout=45000)
            if "ap/signin" in page.url or page.query_selector("input#ap_email"):
                raise SessionExpired("Amazon session expired during shipping check")
            delivery_text = ""
            for selector in ("#mir-layout-DELIVERY_BLOCK", "#deliveryBlock", "#deliverTo"):
                node = page.query_selector(selector)
                if node:
                    delivery_text += " " + node.inner_text()
            normalized = delivery_text.lower()
            is_free = "free delivery" in normalized and "israel" in normalized
            LOGGER.info("Shipping check %s -> free=%s", asin, is_free)
            alerts.extend(state_engine.mark_shipping(asin, is_free))
            time.sleep(random.uniform(10, 20))
        return alerts
    finally:
        close_context(context)

