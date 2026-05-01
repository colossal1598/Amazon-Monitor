import argparse

from main import load_config, run_test_scrape, setup_logging


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one-shot Amazon selector discovery scrape.")
    parser.add_argument("--pages", type=int, default=1, help="Number of search pages to scrape.")
    parser.add_argument("--config", type=str, default="config.yaml", help="Path to monitor config.yaml.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    setup_logging(config.get("log_dir", "logs"))
    run_test_scrape(config, pages_override=args.pages)


if __name__ == "__main__":
    main()
