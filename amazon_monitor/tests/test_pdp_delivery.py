import unittest

from pdp_helpers import _paid_delivery_price_tail, shipping_display_for, shipping_display_hebrew
from pdp_scraper import _extract_pdp_shipping, _pdp_row


class _AttrEl:
    def __init__(self, price: str, text: str = "") -> None:
        self._price = price
        self._text = text

    def get_attribute(self, _name: str) -> str:
        return self._price

    def inner_text(self) -> str:
        return self._text


class _BuyboxRoot:
    def __init__(self, attr_els: list) -> None:
        self._attr_els = attr_els

    def query_selector_all(self, sel: str) -> list:
        if sel == "[data-csa-c-delivery-price]":
            return self._attr_els
        return []


class _ScopedFakePage:
    """Delivery-price attr elements exist ONLY under #buybox; any document-wide
    query_selector_all is an error — that unscoped sweep grabbed a carousel's $6.99
    over the buybox's real $18.52 (live 2026-07-24, B0GW2DK37Q)."""

    def __init__(self, root: _BuyboxRoot | None) -> None:
        self._root = root

    def query_selector(self, sel: str):
        return self._root if sel == "#buybox" else None

    def query_selector_all(self, sel: str) -> list:
        raise AssertionError(f"document-wide sweep must not run: {sel}")


class TestPdpDelivery(unittest.TestCase):
    def test_paid_delivery_line_is_preserved_for_alerts(self) -> None:
        text = "$12.44 delivery Wednesday, June 5\nShips to Israel"
        self.assertEqual(shipping_display_hebrew(text), "משלוח: $12.44")

    def test_paid_ils_delivery_line_is_compacted(self) -> None:
        self.assertEqual(shipping_display_hebrew("Delivery estimate: ILS 54.90"), "משלוח: 54.90")

    def test_free_delivery_line_still_displays_free(self) -> None:
        self.assertEqual(shipping_display_hebrew("FREE delivery Tuesday"), "משלוח חינם")

    def test_multi_amount_delivery_line_anchors_on_delivery_keyword(self) -> None:
        """Real PDP blob: item price + a duplicated echo + an import-charges amount all precede
        the actual delivery charge, which sits right next to the word "delivery"."""
        line = (
            "$109.98 $109 . 98 $47.95 Shipping & Import Charges to Israel Details "
            "$17.96 delivery Wednesday, June 4 to Israel"
        )
        self.assertEqual(_paid_delivery_price_tail(line), "$17.96")
        self.assertEqual(shipping_display_hebrew(line), "משלוח: $17.96")

    def test_first_match_in_line_is_not_blindly_used(self) -> None:
        """The first currency token in the line ($109.98, an item price) is not the delivery price."""
        line = "$109.98 $17.96 delivery Wednesday, June 4 to Israel"
        self.assertEqual(_paid_delivery_price_tail(line), "$17.96")

    def test_no_keyword_falls_back_to_first_amount(self) -> None:
        """Without any delivery/shipping keyword nearby, keep the previous first-match behavior."""
        self.assertEqual(_paid_delivery_price_tail("$12.44 $9.99"), "$12.44")

    def test_date_only_delivery_line_is_omitted(self) -> None:
        """Real SERP regression: a date-only estimate carries no cost info and must not
        be echoed into the WhatsApp message ("משלוח: Delivery Thu, Jul 30")."""
        self.assertEqual(_paid_delivery_price_tail("Delivery Thu, Jul 30"), "")
        self.assertEqual(shipping_display_hebrew("Delivery Thu, Jul 30"), "")

    def test_priced_line_preferred_over_date_only_line(self) -> None:
        text = "Delivery Thu, Jul 30\n$4.99 shipping"
        self.assertEqual(shipping_display_hebrew(text), "משלוח: $4.99")

    def test_delivery_priced_line_beats_import_charges_line(self) -> None:
        """When the tooltip's import-charges figure and the real "$X delivery" charge land
        on SEPARATE lines, the delivery-keyword line must win regardless of order."""
        text = "$47.95 Shipping & Import Charges to Israel Details\n$17.96 delivery Wednesday, June 4"
        self.assertEqual(shipping_display_hebrew(text), "משלוח: $17.96")

    def test_amazon_export_seller_always_free_shipping(self) -> None:
        """Business rule: Amazon Export Sales LLC always ships free — a delivery
        charge shown on the page for an Export-sold item is an Amazon display bug."""
        self.assertEqual(
            shipping_display_for(
                "$12.44 delivery Wednesday",
                seller_text="Ships from Amazon.com Sold by Amazon Export Sales LLC",
            ),
            "משלוח חינם",
        )
        self.assertEqual(
            shipping_display_for("Delivery Thu, Jul 30", seller_text=None, source="aes_llc"),
            "משלוח חינם",
        )

    def test_non_export_seller_keeps_real_shipping(self) -> None:
        self.assertEqual(
            shipping_display_for(
                "$12.44 delivery Wednesday",
                seller_text="Ships from Amazon.com Sold by Amazon.com",
                source="pdp_watch",
            ),
            "משלוח: $12.44",
        )

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


class TestDeliveryAttrScope(unittest.TestCase):
    def test_attr_price_comes_from_buybox_scope_only(self) -> None:
        page = _ScopedFakePage(
            _BuyboxRoot([_AttrEl("$18.52", "$18.52 delivery Wednesday, July 30")])
        )
        text = _extract_pdp_shipping(page)
        self.assertIn("$18.52", text)
        self.assertEqual(shipping_display_hebrew(text), "משלוח: $18.52")

    def test_no_buybox_container_yields_no_shipping_not_a_page_wide_grab(self) -> None:
        page = _ScopedFakePage(None)
        self.assertEqual(_extract_pdp_shipping(page), "")


if __name__ == "__main__":
    unittest.main()
