import unittest

from pdp_helpers import shipping_display_hebrew
from pdp_scraper import _pdp_row


class TestPdpDelivery(unittest.TestCase):
    def test_paid_delivery_line_is_preserved_for_alerts(self) -> None:
        text = "$12.44 delivery Wednesday, June 5\nShips to Israel"
        self.assertEqual(shipping_display_hebrew(text), "משלוח: $12.44")

    def test_paid_ils_delivery_line_is_compacted(self) -> None:
        self.assertEqual(shipping_display_hebrew("Delivery estimate: ILS 54.90"), "משלוח: 54.90")

    def test_free_delivery_line_still_displays_free(self) -> None:
        self.assertEqual(shipping_display_hebrew("FREE delivery Tuesday"), "משלוח חינם")

    def test_paid_delivery_does_not_disqualify_allowed_seller(self) -> None:
        row = _pdp_row(
            "B012345678",
            title="Pokemon TCG Box",
            price=29.99,
            shipping_text="$12.44 delivery Wednesday, June 5",
            image_url=None,
            merchant_blob="Ships from Amazon.com\nSold by Amazon.com",
            allowed=["amazon.com", "amazon export"],
        )
        self.assertTrue(row["in_stock"])
        self.assertEqual(row["price"], 29.99)
        self.assertEqual(row["shipping_text"], "$12.44 delivery Wednesday, June 5")

    def test_not_shippable_still_disqualifies(self) -> None:
        row = _pdp_row(
            "B012345678",
            title="Pokemon TCG Box",
            price=29.99,
            shipping_text="This item cannot be shipped to your selected delivery location.",
            image_url=None,
            merchant_blob="Ships from Amazon.com\nSold by Amazon.com",
            allowed=["amazon.com"],
        )
        self.assertFalse(row["in_stock"])
        self.assertIsNone(row["price"])


if __name__ == "__main__":
    unittest.main()
