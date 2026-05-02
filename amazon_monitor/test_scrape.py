import argparse

from main import load_config, run_scrape_jobs_from_config, run_single_test_scrape_url, run_test_scrape, setup_logging


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one-shot Amazon selector discovery scrape.")
    parser.add_argument("--pages", type=int, default=1, help="Number of search pages to scrape.")
    parser.add_argument("--config", type=str, default="config.yaml", help="Path to monitor config.yaml.")
    parser.add_argument(
        "--url",
        type=str,
        default="",
        help="If set, scrape only this Amazon search URL (writes raw_single_url.json, filtered_single_url.json, …).",
    )
    parser.add_argument(
        "--jobs",
        action="store_true",
        help="Run config.scrape_jobs (each: url, pages, require_free_delivery); same title/blacklist/keywords as monitor, no merchant allowlist.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    setup_logging(config.get("log_dir", "logs"))
    if args.jobs:
        run_scrape_jobs_from_config(config)
        return
    if args.url.strip():
        run_single_test_scrape_url(config, args.url.strip(), pages=args.pages)
        return
    run_test_scrape(config, pages_override=args.pages)


if __name__ == "__main__":
    main()
