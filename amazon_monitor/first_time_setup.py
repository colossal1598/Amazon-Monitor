import yaml
from dotenv import load_dotenv

from webhook_sender import send_alert


def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as file:
        return yaml.safe_load(file)


def run_setup() -> None:
    load_dotenv()
    config = load_config()
    print("Running webhook setup test...")
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
        print("Setup incomplete: verify webhook/n8n flow and rerun setup.")
        return
    print("Setup complete. Webhook flow is working. You can now start main.py.")


if __name__ == "__main__":
    run_setup()

