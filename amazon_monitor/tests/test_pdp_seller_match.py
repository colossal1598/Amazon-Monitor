import unittest

from pdp_scraper import merchant_matches_allowed


class TestPdpSellerMatch(unittest.TestCase):
    def test_amazon_com_substring(self) -> None:
        blob = "Ships from\nAmazon.com\nSold by Amazon.com"
        self.assertTrue(merchant_matches_allowed(blob, ["amazon.com"]))

    def test_amazon_export_variant(self) -> None:
        blob = "Sold by Amazon Export Sales LLC"
        self.assertTrue(merchant_matches_allowed(blob, ["amazon export"]))

    def test_no_match(self) -> None:
        blob = "Sold by Some Other Shop"
        self.assertFalse(merchant_matches_allowed(blob, ["amazon.com", "amazon export"]))
