import requests


# Print the public IP this machine is using so you can confirm what Amazon will see.
def main() -> None:
    response = requests.get("https://checkip.amazonaws.com", timeout=5)
    response.raise_for_status()
    print(response.text.strip())


if __name__ == "__main__":
    main()

