"""PDP helpers: title fallback, continue-shopping detection, debug report."""

from __future__ import annotations

import unittest

from pdp_scraper import (
    _emit_pdp_cycle_debug_report,
    _product_title_from_page_title,
    detect_soft_captcha_from_html,
    pdp_skip_log_label,
)

SOFT_CAPTCHA_HTML = """<!DOCTYPE html><html class="a-no-js" lang="en-us"><head>
<script>ue_sn = "opfcaptcha.amazon.com",</script>
<script src="https://images-na.ssl-images-amazon.com/images/G/01/csminstrumentation/csm-captcha-instrumentation.min.js"></script>
</head><body>
<h4>Click the button below to continue shopping</h4>
</body></html>"""

READY_HTML = """<!DOCTYPE html><html lang="en-us" class="a-js"><head></head><body>
<span id="productTitle">Pokemon TCG Example Box</span>
<div id="desktop_buybox"></div>
</body></html>"""


class ProductTitleFromPageTitleTests(unittest.TestCase):
    def test_amazon_com_colon_product_category(self) -> None:
        raw = "Amazon.com: Pokemon TCG Scarlet Booster : Toys & Games"
        self.assertEqual(
            _product_title_from_page_title(raw),
            "Pokemon TCG Scarlet Booster",
        )

    def test_generic_amazon_title(self) -> None:
        self.assertEqual(_product_title_from_page_title("Amazon.com"), "")

    def test_suffix_amazon_com(self) -> None:
        self.assertEqual(
            _product_title_from_page_title("Widget Name : Amazon.com"),
            "Widget Name",
        )


class PdpContinueShoppingDetectionTests(unittest.TestCase):
    def test_soft_captcha_by_script(self) -> None:
        self.assertTrue(detect_soft_captcha_from_html(SOFT_CAPTCHA_HTML))

    def test_soft_captcha_by_url(self) -> None:
        self.assertTrue(
            detect_soft_captcha_from_html("<html></html>", "https://opfcaptcha.amazon.com/foo")
        )

    def test_ready_page_not_interstitial(self) -> None:
        self.assertFalse(detect_soft_captcha_from_html(READY_HTML))


class PdpSkipLogLabelTests(unittest.TestCase):
    def test_empty_parse_detail(self) -> None:
        row = {"skip_reason": "parse_failed", "skip_detail": "empty_parse"}
        self.assertEqual(pdp_skip_log_label(row), "empty")

    def test_hard_captcha(self) -> None:
        row = {"skip_reason": "captcha", "skip_detail": "robot_check"}
        self.assertEqual(pdp_skip_log_label(row), "captcha")


class PdpDebugReportTests(unittest.TestCase):
    def test_report_includes_ok_and_skip(self) -> None:
        rows = [
            {"asin": "B011111111", "in_stock": True, "price": 19.99, "scrape_attempts": 1},
            {
                "asin": "B022222222",
                "_skip_update": True,
                "skip_reason": "parse_failed",
                "skip_detail": "empty_parse",
                "scrape_attempts": 1,
            },
        ]
        with self.assertLogs("pdp_scraper", level="INFO") as captured:
            _emit_pdp_cycle_debug_report(rows, 12.5)
        text = "\n".join(captured.output)
        self.assertIn("B011111111", text)
        self.assertIn("ok", text)
        self.assertIn("B022222222", text)
        self.assertIn("skip", text)


if __name__ == "__main__":
    unittest.main()
