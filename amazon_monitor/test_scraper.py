"""
test_scraper.py – dump raw product data using existing scraper.
"""
import json
import asyncio
import sys
from pathlib import Path

# Ensure the current folder is on path (though not strictly necessary here)
sys.path.insert(0, str(Path(__file__).resolve().parent))

from search_scraper import scrape_search

TEST_URL = "https://www.amazon.com/s?k=Pokemon+tcg&me=A2XZ7JICGUQ1CX&rh=p_n_is_free_shipping%3A10236242011&s=date-desc-rank"

if __name__ == "__main__":
    products = scrape_search(TEST_URL, pages=2, max_retries=0)  # no retries for test
    for i, item in enumerate(products, 1):
        print(f"\n--- Item {i} ---")
        print(json.dumps(item, indent=2, ensure_ascii=False, default=str))
