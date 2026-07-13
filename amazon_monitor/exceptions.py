class CaptchaBlocked(Exception):
    """Raised when Amazon presents a robot check page."""


class NetworkAccessDenied(Exception):
    """Raised on ERR_NETWORK_ACCESS_DENIED or similar network-level blocks.

    This is retryable — unlike captcha which requires IP rotation.
    """

    # Build a clear error you can log and keep the original failure attached for later troubleshooting.
    def __init__(self, message: str, original_error: Exception | None = None) -> None:
        super().__init__(message)
        self.original_error = original_error


class BrowserDisconnected(Exception):
    """Raised when the Playwright driver/browser connection itself has died.

    Distinct from a single bad page (navigation timeout, parse failure): once the
    underlying Chromium process or driver pipe is gone, *every* subsequent
    ``new_page`` / ``goto`` / DOM call on the same context will fail the same way.
    Treated as fatal for the current browser session so the engine tears the
    session down and relaunches immediately instead of retrying per-ASIN forever.
    """

    def __init__(self, message: str, original_error: Exception | None = None) -> None:
        super().__init__(message)
        self.original_error = original_error


# Substrings matched case-insensitively against a Playwright error message to
# recognize "the whole browser/driver connection is gone" failures (as opposed to
# a single page's own error). Confirmed from production logs, e.g.:
#   "BrowserContext.new_page: Connection closed while reading from the driver"
#   "Page.close: Connection closed while reading from the driver"
#   "Page.title: Connection closed while reading from the driver"
#
# Deliberately NOT included: generic "target page, context or browser has been
# closed" / "browser has been closed" / "browser closed" messages. Playwright
# emits these same generic strings for benign, page-level races too (e.g. a
# call landing just after a single page/tab closed, or an interstitial
# navigation tearing down the execution context) — they do not necessarily mean
# the underlying browser/driver process itself has died. Those are handled by
# the local tolerant retries (see "Execution context was destroyed" / "Target
# closed" / "Target page" handling in search_scraper.py). Misclassifying them
# here as fatal disconnects short-circuits those retries and tears down the
# whole browser session for what is often a one-off hiccup.
_DRIVER_DISCONNECT_PATTERNS = (
    "connection closed while reading from the driver",
    "browser has disconnected",
    "pipe closed",
)


def is_driver_disconnected_error(error: BaseException | str) -> bool:
    """True when an exception (or its message) indicates a dead browser/driver link."""
    err_str = str(error).lower()
    return any(pattern in err_str for pattern in _DRIVER_DISCONNECT_PATTERNS)
