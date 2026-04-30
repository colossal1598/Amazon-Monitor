class CaptchaBlocked(Exception):
    """Raised when Amazon presents a robot check page."""


class ModemIPUnchanged(Exception):
    """Raised when modem reconnect did not produce a new public IP."""
