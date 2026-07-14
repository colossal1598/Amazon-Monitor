import logging
import time
from datetime import datetime, timezone
from typing import Any

import requests

import fx_rate
import image_cache

LOGGER = logging.getLogger(__name__)


# Send a WhatsApp-style message to your webhook, retrying transient failures.
def _post_wa(config: dict[str, Any], payload: dict[str, Any]) -> bool:
    """POST to the wa-server; returns True on success.

    A restock alert is time-critical and the alert row is already recorded in the
    DB before dispatch (cooldowns armed), so a dropped send is silently lost to the
    client forever. Production 2026-07-15: wa-server returned 500 on every call for
    minutes and every alert in that window vanished. Bounded retries with backoff
    cover short wa-server restarts/hiccups; a persistently dead wa-server still
    fails after the last attempt (logged loudly) rather than blocking the engine.
    """
    url = config.get("wa_api_url")
    api_key = config.get("wa_api_key")
    if not url:
        LOGGER.warning("wa_api_url is not configured; skipping send.")
        return False
    headers = {}
    if api_key:
        headers["x-api-key"] = api_key
    try:
        attempts = max(1, int(config.get("wa_send_attempts", 3)))
    except (TypeError, ValueError):
        attempts = 3
    try:
        backoff_base = max(0.5, float(config.get("wa_send_retry_backoff_seconds", 2.0)))
    except (TypeError, ValueError):
        backoff_base = 2.0
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=10)
            response.raise_for_status()
            if attempt > 1:
                LOGGER.info("WhatsApp send succeeded on retry %s/%s", attempt, attempts)
            return True
        except Exception as exc:  # noqa: BLE001 - any send failure is retryable here
            last_exc = exc
            if attempt < attempts:
                LOGGER.warning(
                    "WhatsApp API call failed (%s) attempt %s/%s, retrying: %s",
                    url,
                    attempt,
                    attempts,
                    exc,
                )
                time.sleep(backoff_base * attempt)
    LOGGER.error(
        "WhatsApp API call failed after %s attempts (%s): %s — ALERT LOST: %s",
        attempts,
        url,
        last_exc,
        str(payload.get("message", ""))[:120],
    )
    return False


# YAML may only override these product-facing templates; operational alerts always use strings below.
USER_OVERRIDABLE_MESSAGE_KEYS = frozenset(
    {"default", "new_product", "price_drop", "back_in_stock", "price_below_target"}
)

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
    "price_below_target": (
        "המחיר ירד מתחת למחיר היעד ({target_price}$)!\n"
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
    "pdp_error": "PDP loop error: {error_message}\nTime: {timestamp}",
    "modem_error": "Modem job error: {error_message}\nTime: {timestamp}",
    "modem_trigger": "Modem recovery trigger fired.\nTime: {timestamp}",
}


class _SafeDict(dict):
    # Prevent missing template fields from crashing message formatting by returning an empty string instead.
    def __missing__(self, key: str) -> str:
        return ""


# Turn a stored numeric price into a friendly “$12.34” string (or “not available” when we can’t).
def _format_usd(value: Any) -> str:
    """Always show product price as USD with $ prefix (numeric alerts are stored as USD)."""
    if value is None:
        return "לא זמין"
    try:
        return f"${float(value):.2f}"
    except (TypeError, ValueError):
        return "לא זמין"


# Build the price line for alerts by showing USD and optionally adding an approximate ILS amount when you configured exchange rate support.
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


# Turn a target price number into the bare "12.34" text used before the "$" in the price_below_target template.
def _format_target_price(value: Any) -> str:
    if value is None:
        return ""
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return ""


# Create the final WhatsApp message text by choosing the right template and filling in the alert details.
def _format_message(alert_payload: dict[str, Any], config: dict[str, Any]) -> str:
    alert_type = str(alert_payload.get("type") or "default")
    price = alert_payload.get("price")
    old_price = alert_payload.get("old_price")
    new_price = alert_payload.get("new_price")
    price_text, price_text_usd_only, price_text_ils_suffix = _price_line_parts(
        new_price if new_price is not None else price, config
    )

    templates = DEFAULT_MESSAGE_TEMPLATES.copy()
    user_templates = config.get("wa_message_templates")
    if isinstance(user_templates, dict):
        for key, val in user_templates.items():
            if key in USER_OVERRIDABLE_MESSAGE_KEYS:
                templates[key] = str(val)

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
            "target_price": _format_target_price(alert_payload.get("target_price")),
        }
    )
    return template.format_map(values)


# Choose where to send messages (group vs personal) based on whether it’s a product alert or an operational message.
def _pick_recipient(config: dict[str, Any], operational: bool = False) -> str | None:
    if operational:
        return config.get("wa_client_to") or config.get("wa_group_id")
    return config.get("wa_group_id")


# Turn an internal alert dict into a WhatsApp webhook payload (including affiliate link) and send it out.
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
        "target_price": alert_dict.get("target_price"),
    }
    wa_payload = {
        "to": _pick_recipient(config, operational=False),
        "message": _format_message(payload, config),
    }
    image_url = alert_dict.get("image_url")
    has_image = False
    if asin:
        cached = image_cache.ensure_cached_image(str(asin), image_url, config)
        if cached and cached.is_file():
            wa_payload["image_path"] = str(cached.resolve())
            has_image = True
        elif isinstance(image_url, str) and image_url.startswith(("http://", "https://")):
            wa_payload["image_url"] = image_url
            has_image = True
    elif isinstance(image_url, str) and image_url.startswith(("http://", "https://")):
        wa_payload["image_url"] = image_url
        has_image = True
    if not has_image:
        LOGGER.warning(
            "Alert has no image (asin=%s, type=%s): no cached file and no usable image_url.",
            asin,
            alert_dict.get("type"),
        )
    _post_wa(config, wa_payload)


# Send a small “I’m alive” heartbeat message on a schedule when you enabled it in the config.
def send_heartbeat(config: dict[str, Any]) -> None:
    if not config.get("wa_send_heartbeat", False):
        return
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "to": _pick_recipient(config, operational=True),
        "message": _format_message({"type": "heartbeat_ok", "timestamp": now}, config),
    }
    _post_wa(config, payload)


# Keep this function as a harmless placeholder so old configs don’t break, even though modem rotation is no longer used.
def send_modem_trigger(config: dict[str, Any]) -> None:
    """No-op: modem rotation was removed from the monitor."""
    LOGGER.debug("send_modem_trigger ignored (modem flow removed)")


# Send an operational error message (like search/heartbeat problems) so you’ll notice failures even when no product alerts fire.
def send_operational_error(event_type: str, error_message: str, config: dict[str, Any]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "to": _pick_recipient(config, operational=True),
        "message": _format_message({"type": event_type, "error_message": error_message, "timestamp": now}, config),
    }
    _post_wa(config, payload)


def send_client_message(message: str, config: dict[str, Any]) -> None:
    """Send a plain-text message to wa_client_to (personal DM)."""
    to = str(config.get("wa_client_to") or "").strip()
    if not to:
        LOGGER.warning("wa_client_to is not configured; skipping client message.")
        return
    _post_wa(config, {"to": to, "message": message})
