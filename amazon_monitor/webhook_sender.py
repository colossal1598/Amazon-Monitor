import logging
from datetime import datetime, timezone
from typing import Any

import requests

LOGGER = logging.getLogger(__name__)


def _post(url: str | None, payload: dict[str, Any]) -> None:
    if not url:
        return
    try:
        response = requests.post(url, json=payload, timeout=5)
        response.raise_for_status()
    except Exception as exc:
        LOGGER.warning("Webhook call failed (%s): %s", url, exc)


def send_alert(alert_dict: dict[str, Any], config: dict[str, Any]) -> None:
    asin = alert_dict.get("asin")
    tag = config.get("affiliate_tag", "")
    payload = {
        "type": alert_dict.get("type"),
        "asin": asin,
        "title": alert_dict.get("title"),
        "price": alert_dict.get("price"),
        "old_price": alert_dict.get("old_price"),
        "new_price": alert_dict.get("new_price"),
        "pct_drop": alert_dict.get("percentage"),
        "source": alert_dict.get("source"),
        "image_url": alert_dict.get("image_url"),
        "affiliate_link": f"https://www.amazon.com/dp/{asin}?tag={tag}" if asin else None,
        "timestamp": alert_dict.get("timestamp") or datetime.now(timezone.utc).isoformat(),
    }
    _post(config.get("webhook_alert"), payload)


def send_heartbeat(config: dict[str, Any]) -> None:
    payload = {"type": "heartbeat", "timestamp": datetime.now(timezone.utc).isoformat()}
    _post(config.get("webhook_heartbeat"), payload)


def send_modem_trigger(config: dict[str, Any]) -> None:
    payload = {"type": "modem_trigger", "timestamp": datetime.now(timezone.utc).isoformat()}
    _post(config.get("webhook_modem_trigger"), payload)
