"""Test script to verify modem restart command works correctly."""

import subprocess
import sys
import time

import requests


def get_current_ip() -> str | None:
    """Get current public IP."""
    try:
        r = requests.get("https://checkip.amazonaws.com", timeout=10)
        return r.text.strip()
    except Exception as e:
        print(f"Failed to get IP: {e}")
        return None


def run_modem_command(command: str) -> tuple[bool, str]:
    """Run modem command and return success status + output."""
    print(f"\nRunning: {command}")
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            print("Command succeeded")
            return True, result.stdout
        else:
            print(f"Command failed (exit {result.returncode}):")
            print(result.stderr)
            return False, result.stderr
    except subprocess.TimeoutExpired:
        print("Command timed out after 120s")
        return False, "timeout"
    except Exception as e:
        print(f"Command failed with exception: {e}")
        return False, str(e)


def main() -> int:
    # Load config
    import yaml
    try:
        with open("config.yaml", "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
    except Exception as e:
        print(f"Failed to load config.yaml: {e}")
        return 1

    command = config.get("modem_reconnect_command", "").strip()
    if not command:
        print("ERROR: modem_reconnect_command is empty in config.yaml")
        return 1

    print("=" * 60)
    print("MODEM RESTART TEST")
    print("=" * 60)

    # Get IP before
    print("\n[1/4] Getting current public IP...")
    ip_before = get_current_ip()
    if ip_before:
        print(f"  Current IP: {ip_before}")
    else:
        print("  Could not determine current IP")

    # Run modem command
    print("\n[2/4] Running modem reconnect command...")
    success, output = run_modem_command(command)
    if not success:
        print("\nFAILED: Modem command did not execute successfully")
        return 1

    # Wait for reconnect
    print("\n[3/4] Waiting 15 seconds for modem to reconnect...")
    time.sleep(15)

    # Get IP after
    print("\n[4/4] Getting new public IP...")
    ip_after = get_current_ip()
    if ip_after:
        print(f"  New IP: {ip_after}")
    else:
        print("  Could not determine new IP")
        return 1

    # Compare
    print("\n" + "=" * 60)
    if ip_before and ip_after and ip_before != ip_after:
        print("SUCCESS: IP changed!")
        print(f"  Before: {ip_before}")
        print(f"  After:  {ip_after}")
        return 0
    elif ip_before == ip_after:
        print("FAILED: IP did not change")
        print(f"  IP stayed: {ip_before}")
        return 1
    else:
        print("UNCERTAIN: Could not compare IPs")
        return 1


if __name__ == "__main__":
    sys.exit(main())
