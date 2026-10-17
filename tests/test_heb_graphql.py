"""
tests/test_heb_graphql.py

Unit tests for H-E-B GraphQL Engine & CDP Session Harvester:
- CDPSessionHarvester: CDP attach, rookiepy extraction, and cache fallback
- HEBGraphQLClient: persisted queries (cartEstimated, cartItemV2, CouponClip)
- Confirmation Gates: confirm=True enforcement on cart/coupon mutations
- Safety Invariants: HEBCheckoutGateError blocking checkout/payment mutations
- HEBClient Integration: delegated cart and coupon methods
"""

import os
import sys
import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.cdp_harvester import CDPSessionHarvester, SessionState
from scripts.heb_graphql import (
    HEBGraphQLClient,
    HEBGraphQLError,
    HEBCheckoutGateError,
    HEBWAFChallengeError,
    HEBAuthenticationError,
    PERSISTED_QUERIES,
)
from scripts.heb_client import HEBClient


class TestCDPSessionHarvester(unittest.TestCase):
    """Test CDP and fallback cookie harvesting logic."""

    def setUp(self):
        self.temp_session_file = Path("/tmp/test_heb_session.json")
        if self.temp_session_file.exists():
            self.temp_session_file.unlink()
        self.harvester = CDPSessionHarvester(session_path=self.temp_session_file)

    def tearDown(self):
        if self.temp_session_file.exists():
            self.temp_session_file.unlink()

    def test_session_state_cookie_header(self):
        state = SessionState(
            source="test",
            cookies={"reese84": "token123", "sst": "auth456"},
        )
        header = state.get_cookie_header()
        self.assertIn("reese84=token123", header)
        self.assertIn("sst=auth456", header)

    @patch.object(CDPSessionHarvester, "is_cdp_available", return_value=False)
    @patch.object(CDPSessionHarvester, "_harvest_from_rookiepy")
    def test_harvest_rookiepy_fallback(self, mock_rookie, mock_cdp):
        mock_rookie.return_value = SessionState(
            source="rookiepy",
            cookies={"sst": "valid_token", "reese84": "waf_pass"},
            raw_cookies=[{"name": "sst", "value": "valid_token"}],
            is_authenticated=True,
            has_reese84=True,
            has_sst=True,
        )
        state = self.harvester.harvest(ensure_warmup=False)
        self.assertEqual(state.source, "rookiepy")
        self.assertTrue(state.is_authenticated)
        self.assertTrue(state.has_reese84)
        # Check persistence
        self.assertTrue(self.temp_session_file.exists())

    @patch.object(CDPSessionHarvester, "is_cdp_available", return_value=False)
    @patch.object(CDPSessionHarvester, "_harvest_from_rookiepy", return_value=None)
    def test_harvest_cache_fallback(self, mock_rookie, mock_cdp):
        cached_data = [
            {"name": "sst", "value": "cached_sst_token"},
            {"name": "reese84", "value": "cached_reese_token"},
        ]
        with open(self.temp_session_file, "w") as f:
            json.dump(cached_data, f)

        state = self.harvester.harvest(ensure_warmup=False)
        self.assertEqual(state.source, "cache")
        self.assertEqual(state.cookies.get("sst"), "cached_sst_token")
        self.assertTrue(state.is_authenticated)


class TestHEBGraphQLClient(unittest.TestCase):
    """Test GraphQL persisted queries, confirm gates, and safety blocks."""

    def setUp(self):
        self.client = HEBGraphQLClient(store_id="639")
        self.mock_session = SessionState(
            source="mock",
            cookies={"sst": "test_auth", "reese84": "test_waf"},
            user_agent="MockAgent/1.0",
        )

    def test_strict_safety_invariant_blocks_checkout(self):
        """Verify that checkout/payment operations are strictly blocked with HEBCheckoutGateError."""
        forbidden_calls = [
            "checkout",
            "place_order",
            "submit_payment",
            "finalize_order",
            "pay",
            "book_delivery",
        ]
        for op in forbidden_calls:
            with self.subTest(operation=op):
                with self.assertRaises(HEBCheckoutGateError):
                    self.client.execute_persisted_query(op, {}, session_state=self.mock_session)

    def test_cart_get_normalization(self):
        """Verify that cart_get parses cartEstimated response into normalized items."""
        mock_cart_data = {
            "cartV2": {
                "items": [
                    {
                        "product": {
                            "id": "127074",
                            "fullDisplayName": "H-E-B Organic Whole Milk",
                            "SKUs": [{"id": "4122071073", "customerFriendlySize": "1 gal"}],
                        },
                        "quantity": 2,
                        "unitPrice": 4.98,
                    }
                ]
            }
        }
        with patch.object(self.client, "execute_persisted_query", return_value=mock_cart_data):
            cart = self.client.cart_get()
            self.assertEqual(cart["status"], "SUCCESS")
            self.assertEqual(cart["item_count"], 1)
            self.assertEqual(cart["subtotal"], 9.96)
            self.assertEqual(cart["items"][0]["product_id"], "127074")
            self.assertEqual(cart["items"][0]["quantity"], 2)

    def test_cart_add_confirm_gate(self):
        """Verify cart_add returns preview when confirm=False and executes when confirm=True."""
        # confirm=False (Preview mode)
        preview = self.client.cart_add("127074", sku_id="4122071073", quantity=1, confirm=False)
        self.assertTrue(preview.get("preview"))
        self.assertEqual(preview.get("action"), "cart_add")
        self.assertEqual(preview.get("product_id"), "127074")

        # confirm=True (Execution mode)
        mock_verify_cart = {
            "status": "SUCCESS",
            "item_count": 1,
            "subtotal": 4.98,
            "items": [{"product_id": "127074", "sku_id": "4122071073", "quantity": 1}],
        }
        with patch.object(self.client, "execute_persisted_query", return_value={"cartItemV2": {"ok": True}}):
            with patch.object(self.client, "cart_get", return_value=mock_verify_cart):
                result = self.client.cart_add("127074", sku_id="4122071073", quantity=1, confirm=True)
                self.assertEqual(result["status"], "SUCCESS")
                self.assertTrue(result["verified_in_cart"])

    def test_cart_remove_confirm_gate(self):
        """Verify cart_remove returns preview when confirm=False and sends quantity=0 when confirm=True."""
        preview = self.client.cart_remove("127074", confirm=False)
        self.assertTrue(preview.get("preview"))
        self.assertEqual(preview.get("action"), "cart_remove")

        mock_empty_cart = {"status": "SUCCESS", "item_count": 0, "subtotal": 0.0, "items": []}
        with patch.object(self.client, "execute_persisted_query") as mock_exec:
            mock_exec.return_value = {"cartItemV2": {"ok": True}}
            with patch.object(self.client, "cart_get", return_value=mock_empty_cart):
                res = self.client.cart_remove("127074", sku_id="4122071073", confirm=True)
                self.assertEqual(res["status"], "SUCCESS")
                # Ensure variables contained quantity: 0
                mock_exec.assert_called_once()
                args, kwargs = mock_exec.call_args
                self.assertEqual(args[0], "cartItemV2")
                self.assertEqual(args[1]["quantity"], 0)

    def test_coupon_clip_confirm_gate(self):
        """Verify coupon_clip requires confirm=True."""
        preview = self.client.coupon_clip(102938, confirm=False)
        self.assertTrue(preview.get("preview"))
        self.assertEqual(preview.get("coupon_id"), 102938)

        with patch.object(self.client, "execute_persisted_query", return_value={"clipCoupon": {"ok": True}}):
            res = self.client.coupon_clip(102938, confirm=True)
            self.assertEqual(res["status"], "SUCCESS")
            self.assertEqual(res["coupon_id"], 102938)

    def test_parse_next_data_html(self):
        """Verify __NEXT_DATA__ JSON extraction from search results HTML."""
        sample_next_data = {
            "props": {
                "pageProps": {
                    "layout": {
                        "visualComponents": [
                            {
                                "type": "searchGridV2",
                                "items": [
                                    {
                                        "__typename": "Product",
                                        "id": "590123",
                                        "fullDisplayName": "H-E-B Jasmine Rice",
                                        "brand": {"name": "H-E-B"},
                                        "SKUs": [
                                            {
                                                "id": "SKU_JAS_1",
                                                "customerFriendlySize": "32 oz",
                                                "contextPrices": [
                                                    {"context": "CURBSIDE", "salePrice": {"amount": "3.98"}}
                                                ],
                                            }
                                        ],
                                        "inventory": {"inventoryState": "IN_STOCK"},
                                    }
                                ],
                            }
                        ]
                    }
                }
            }
        }
        html = f'<html><head><script id="__NEXT_DATA__" type="application/json">{json.dumps(sample_next_data)}</script></head><body></body></html>'
        products = self.client._parse_next_data_html(html)
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0]["product_id"], "590123")
        self.assertEqual(products[0]["name"], "H-E-B Jasmine Rice")
        self.assertEqual(products[0]["price"], 3.98)
        self.assertTrue(products[0]["in_stock"])


class TestHEBClientIntegration(unittest.TestCase):
    """Test HEBClient high-level delegation to GraphQL operations."""

    def setUp(self):
        self.client = HEBClient(store_id="639")

    def test_client_delegates_cart_get(self):
        with patch.object(self.client.graphql_client, "cart_get", return_value={"item_count": 0}) as mock_cg:
            res = self.client.cart_get()
            mock_cg.assert_called_once()
            self.assertEqual(res["item_count"], 0)

    def test_client_delegates_cart_add(self):
        with patch.object(self.client.graphql_client, "cart_add", return_value={"preview": True}) as mock_ca:
            res = self.client.cart_add("123", confirm=False)
            mock_ca.assert_called_once_with(product_id="123", sku_id=None, quantity=1, confirm=False)
            self.assertTrue(res["preview"])

    def test_client_delegates_coupon_clip(self):
        with patch.object(self.client.graphql_client, "coupon_clip", return_value={"preview": True}) as mock_cc:
            res = self.client.coupon_clip(999, confirm=False)
            mock_cc.assert_called_once_with(coupon_id=999, confirm=False)
            self.assertTrue(res["preview"])

    def test_extract_product_cards_skips_invalid_cards_without_synthetic_fallback(self):
        """Verify that product cards missing product-detail href are discarded, never assigned synthetic IDs."""
        mock_page = MagicMock()
        mock_card_invalid = MagicMock()
        mock_card_invalid.query_selector.side_effect = lambda sel: (
            MagicMock(inner_text=lambda: "Mystery Product") if "title" in sel
            else MagicMock(inner_text=lambda: "$5.99") if "price" in sel
            else MagicMock(get_attribute=lambda attr: "/non-product-link") if "href" in sel
            else None
        )
        mock_page.query_selector_all.return_value = [mock_card_invalid]

        extracted = self.client._extract_product_cards(mock_page)
        # Should be empty because href does not contain a numeric product-detail ID
        self.assertEqual(len(extracted), 0)

    def test_search_heb_product_propagates_waf_loudly(self):
        """Verify that when GraphQL and Playwright hit a WAF challenge, it raises loudly."""
        with patch.object(self.client.graphql_client, "product_search", side_effect=HEBWAFChallengeError("Imperva challenge")):
            with patch.object(self.client, "_search_via_playwright", side_effect=HEBWAFChallengeError("Playwright challenge")):
                with self.assertRaises(HEBWAFChallengeError):
                    self.client.search_heb_product("salmon")

    def test_cache_purity_no_poisoned_skus(self):
        """Verify that cache/recipes and index.json contain zero synthetic mock SKUs."""
        cache_recipes_dir = Path(__file__).resolve().parent.parent / "cache" / "recipes"
        index_file = Path(__file__).resolve().parent.parent / "cache" / "index.json"

        # Check recipe files if any exist
        if cache_recipes_dir.exists():
            for recipe_file in cache_recipes_dir.glob("*.yaml"):
                content = recipe_file.read_text(encoding="utf-8")
                self.assertNotIn("prod_", content)
                self.assertNotIn("590328", content)
                self.assertNotIn("716744", content)
                self.assertNotIn("761531", content)

        if index_file.exists():
            content = index_file.read_text(encoding="utf-8")
            self.assertNotIn("prod_", content)
            self.assertNotIn("590328", content)


if __name__ == "__main__":
    unittest.main()
