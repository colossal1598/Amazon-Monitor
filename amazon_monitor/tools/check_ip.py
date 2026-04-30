import requests


def main() -> None:
    response = requests.get("https://checkip.amazonaws.com", timeout=5)
    response.raise_for_status()
    print(response.text.strip())


if __name__ == "__main__":
    main()

