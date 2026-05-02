class CaptchaBlocked(Exception):
    """Raised when Amazon presents a robot check page."""


class NetworkAccessDenied(Exception):
    """Raised on ERR_NETWORK_ACCESS_DENIED or similar network-level blocks.

    This is retryable — unlike captcha which requires IP rotation.
    """

    def __init__(self, message: str, original_error: Exception | None = None) -> None:
        super().__init__(message)
        self.original_error = original_error
