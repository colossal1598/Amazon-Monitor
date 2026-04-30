import logging
from datetime import datetime, timezone
from typing import Any

import requests

LOGGER = logging.getLogger(__name__)


def _post_wa(config: dict[str, Any], payload: dict[str, Any]) -> None:
    url = config.get("wa_api_url")
    api_key = config.get("wa_api_key")
    if not url:
        LOGGER.warning("wa_api_url is not configured; skipping send.")
        return
    headers = {}
    if api_key:
        headers["x-api-key"] = api_key
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        response.raise_for_status()
    except Exception as exc:
        LOGGER.warning("WhatsApp API call failed (%s): %s", url, exc)


def _format_message(alert_payload: dict[str, Any]) -> str:
    alert_type = alert_payload.get("type")
    title = alert_payload.get("title") or "Untitled item"
    price = alert_payload.get("price")
    old_price = alert_payload.get("old_price")
    new_price = alert_payload.get("new_price")
    affiliate_link = alert_payload.get("affiliate_link")

    if alert_type == "price_drop":
        line1 = "Price drop detected!"
        price_text = f"{old_price} -> {new_price or price}" if old_price else str(new_price or price)
    elif alert_type == "back_in_stock":
        line1 = "Back in stock!"
        price_text = str(price)
    elif alert_type == "setup_test":
        line1 = "Setup test alert"
        price_text = str(price)
    else:
        line1 = "New product detected!"
        price_text = str(price)

    return (
        f"{line1}\n"
        f"Title: {title}\n"
        f"Price: {price_text}\n"
        f"Link: {affiliate_link}"
    )


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
    wa_payload = {
        "to": config.get("wa_group_id"),
        "message": _format_message(payload),
    }
    image_url = payload.get("image_url")
    if isinstance(image_url, str) and image_url.startswith(("http://", "https://")):
        wa_payload["image_url"] = image_url
    _post_wa(config, wa_payload)


def send_heartbeat(config: dict[str, Any]) -> None:
    if not config.get("wa_send_heartbeat", False):
        return
    payload = {
        "to": config.get("wa_group_id"),
        "message": f"Monitor heartbeat OK ({datetime.now(timezone.utc).isoformat()})",
    }
    _post_wa(config, payload)


def send_modem_trigger(config: dict[str, Any]) -> None:
    payload = {
        "to": config.get("wa_group_id"),
        "message": f"Modem recovery trigger fired ({datetime.now(timezone.utc).isoformat()})",
    }
    _post_wa(config, payload)
