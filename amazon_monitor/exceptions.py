class CaptchaBlocked(Exception):
    """Raised when Amazon presents a robot check page."""


class SessionExpired(Exception):
    """Raised when the persistent Amazon session is no longer authenticated."""


class ModemIPUnchanged(Exception):
    """Raised when modem reconnect did not produce a new public IP."""
