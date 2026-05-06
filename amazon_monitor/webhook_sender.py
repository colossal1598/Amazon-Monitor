import logging
from datetime import datetime, timezone
from typing import Any

import requests

import fx_rate

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


DEFAULT_MESSAGE_TEMPLATES = {
    "default": (
        "New product detected!\n"
        "Title: {title}\n"
        "Price: {price_text}\n"
        "{shipping}\n"
        "Link: {affiliate_link}"
    ),
    "new_product": (
        "New product detected!\n"
        "Title: {title}\n"
        "Price: {price_text}\n"
        "{shipping}\n"
        "Link: {affiliate_link}"
    ),
    "price_drop": (
        "Price drop detected!\n"
        "Title: {title}\n"
        "Price: {price_text}\n"
        "{shipping}\n"
        "Link: {affiliate_link}"
    ),
    "back_in_stock": (
        "Back in stock!\n"
        "Title: {title}\n"
        "Price: {price_text}\n"
        "{shipping}\n"
        "Link: {affiliate_link}"
    ),
    "setup_test": (
        "Setup test alert\n"
        "Title: {title}\n"
        "Price: {price_text}\n"
        "{shipping}\n"
        "Link: {affiliate_link}"
    ),
    "heartbeat_ok": "Heartbeat OK: {timestamp}",
    "heartbeat_error": "Heartbeat failed: {error_message}\nTime: {timestamp}",
    "search_error": "Search loop error: {error_message}\nTime: {timestamp}",
    "modem_error": "Modem job error: {error_message}\nTime: {timestamp}",
    "modem_trigger": "Modem recovery trigger fired.\nTime: {timestamp}",
}


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return ""


def _format_usd(value: Any) -> str:
    """Always show product price as USD with $ prefix (numeric alerts are stored as USD)."""
    if value is None:
        return "לא זמין"
    try:
        return f"${float(value):.2f}"
    except (TypeError, ValueError):
        return "לא זמין"


def _price_line_parts(amount: Any, config: dict[str, Any]) -> tuple[str, str, str]:
    """Returns (full_line_with_optional_ils, usd_only, ils_suffix_or_empty)."""
    usd = _format_usd(amount)
    if usd == "לא זמין":
        return usd, usd, ""
    rate = fx_rate.get_usd_ils(config)
    if rate is None or rate <= 0:
        return usd, usd, ""
    try:
        ils = int(round(float(amount) * rate))
    except (TypeError, ValueError):
        return usd, usd, ""
    suffix = f" (כ- {ils}₪)"
    return usd + suffix, usd, suffix


def _format_message(alert_payload: dict[str, Any], config: dict[str, Any]) -> str:
    alert_type = str(alert_payload.get("type") or "default")
    price = alert_payload.get("price")
    old_price = alert_payload.get("old_price")
    new_price = alert_payload.get("new_price")
    if alert_type == "price_drop" and old_price is not None:
        left_full, usd_l, ils_l = _price_line_parts(old_price, config)
        right_full, usd_r, ils_r = _price_line_parts(
            new_price if new_price is not None else price, config
        )
        price_text = f"{left_full} -> {right_full}"
        price_text_usd_only = f"{usd_l} -> {usd_r}"
        if ils_l and ils_r:
            price_text_ils_suffix = f"{ils_l} -> {ils_r}"
        elif ils_l:
            price_text_ils_suffix = ils_l
        elif ils_r:
            price_text_ils_suffix = ils_r
        else:
            price_text_ils_suffix = ""
    else:
        price_text, price_text_usd_only, price_text_ils_suffix = _price_line_parts(
            new_price if new_price is not None else price, config
        )

    templates = DEFAULT_MESSAGE_TEMPLATES.copy()
    user_templates = config.get("wa_message_templates")
    if isinstance(user_templates, dict):
        templates.update({k: str(v) for k, v in user_templates.items()})

    template = templates.get(alert_type) or templates["default"]
    values = _SafeDict(
        {
            "type": alert_type,
            "asin": alert_payload.get("asin"),
            "title": alert_payload.get("title") or "Untitled item",
            "price": price,
            "old_price": _format_usd(old_price),
            "new_price": _format_usd(new_price if new_price is not None else price),
            "pct_drop": alert_payload.get("pct_drop"),
            "source": alert_payload.get("source"),
            "image_url": alert_payload.get("image_url"),
            "affiliate_link": alert_payload.get("affiliate_link"),
            "timestamp": alert_payload.get("timestamp"),
            "price_text": price_text,
            "price_text_usd_only": price_text_usd_only,
            "price_text_ils_suffix": price_text_ils_suffix,
            "error_message": alert_payload.get("error_message"),
            "shipping": (alert_payload.get("shipping") or "").strip(),
        }
    )
    return template.format_map(values)


def _pick_recipient(config: dict[str, Any], operational: bool = False) -> str | None:
    if operational:
        return config.get("wa_client_to") or config.get("wa_group_id")
    return config.get("wa_group_id")


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
        "shipping": alert_dict.get("shipping"),
    }
    wa_payload = {
        "to": _pick_recipient(config, operational=False),
        "message": _format_message(payload, config),
    }
    image_url = payload.get("image_url")
    if isinstance(image_url, str) and image_url.startswith(("http://", "https://")):
        wa_payload["image_url"] = image_url
    _post_wa(config, wa_payload)


def send_heartbeat(config: dict[str, Any]) -> None:
    if not config.get("wa_send_heartbeat", False):
        return
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "to": _pick_recipient(config, operational=True),
        "message": _format_message({"type": "heartbeat_ok", "timestamp": now}, config),
    }
    _post_wa(config, payload)


def send_modem_trigger(config: dict[str, Any]) -> None:
    """No-op: modem rotation was removed from the monitor."""
    LOGGER.debug("send_modem_trigger ignored (modem flow removed)")


def send_operational_error(event_type: str, error_message: str, config: dict[str, Any]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "to": _pick_recipient(config, operational=True),
        "message": _format_message({"type": event_type, "error_message": error_message, "timestamp": now}, config),
    }
    _post_wa(config, payload)
