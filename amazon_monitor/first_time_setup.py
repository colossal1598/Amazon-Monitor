from pathlib import Path

import yaml
from dotenv import load_dotenv

from browser_factory import close_context, create_stealth_context
from webhook_sender import send_alert


def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as file:
        return yaml.safe_load(file)


def run_setup() -> None:
    load_dotenv()
    config = load_config()
    auth_root = Path(config.get("auth_dir", "auth"))
    amazon_auth = auth_root / "amazon"
    amazon_auth.mkdir(parents=True, exist_ok=True)

    print("Setting up Amazon session...")
    context = create_stealth_context(persistent_dir=str(amazon_auth), headless=False)
    try:
        page = context.new_page()
        page.goto("https://www.amazon.com/ap/signin", wait_until="domcontentloaded")
        input("Complete login/2FA in opened browser, then press Enter to continue...")
        print("Please verify Israeli shipping address is default.")
        page.goto("https://www.amazon.com/gp/cart/view.html", wait_until="domcontentloaded")
        input("Confirm cart page loads, then press Enter to close setup browser...")
    finally:
        close_context(context)

    print("Testing n8n webhook...")
    dummy_alert = {
        "type": "setup_test",
        "asin": "B000000000",
        "title": "Setup Test Alert",
        "price": 0.0,
        "old_price": None,
        "new_price": None,
        "percentage": None,
        "source": "setup",
        "image_url": "https://m.media-amazon.com/images/I/51example.jpg",
    }
    send_alert(dummy_alert, config)
    confirmed = input("Did you receive the WhatsApp message via n8n? (y/n): ").strip().lower()
    if confirmed != "y":
        print("Setup incomplete: verify webhook or n8n flow and rerun setup.")
        return
    print("Setup complete. Now manually add priority ASINs to the cart and start main.py.")


if __name__ == "__main__":
    run_setup()

