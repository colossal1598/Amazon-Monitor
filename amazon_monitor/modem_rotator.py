import logging
import os
import subprocess
import time
from pathlib import Path

import requests

from exceptions import ModemIPUnchanged

LOGGER = logging.getLogger(__name__)


def _get_public_ip() -> str:
    response = requests.get("https://checkip.amazonaws.com", timeout=5)
    response.raise_for_status()
    return response.text.strip()


def _wait_for_internet(max_wait_seconds: int = 90) -> None:
    ping_command = ["ping", "-n" if os.name == "nt" else "-c", "1", "8.8.8.8"]
    deadline = time.time() + max_wait_seconds
    while time.time() < deadline:
        result = subprocess.run(ping_command, capture_output=True, text=True)
        if result.returncode == 0:
            return
        time.sleep(2)
    raise TimeoutError("Internet did not return after modem reconnect")


def reconnect_modem(config: dict) -> str:
    command = config["modem_reconnect_command"]
    last_ip_file = Path(config.get("last_ip_path", "data/last_ip.txt"))
    last_ip_file.parent.mkdir(parents=True, exist_ok=True)
    old_ip = last_ip_file.read_text(encoding="utf-8").strip() if last_ip_file.exists() else ""
    if not old_ip:
        try:
            old_ip = _get_public_ip()
        except Exception:
            old_ip = ""

    for attempt in range(2):
        LOGGER.warning("Running modem reconnect command (attempt %s)", attempt + 1)
        subprocess.run(command, shell=True, check=False)
        _wait_for_internet()
        new_ip = _get_public_ip()
        if new_ip and new_ip != old_ip:
            last_ip_file.write_text(new_ip, encoding="utf-8")
            LOGGER.info("Modem reconnect succeeded with new IP: %s", new_ip)
            return new_ip
    LOGGER.error("Modem reconnect failed to change IP")
    raise ModemIPUnchanged("Public IP did not change after modem reconnect")
