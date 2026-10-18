"""
tests/test_past_orders.py

Unit tests for H-E-B Past Orders and "Buy It Again" API integrations:
- Normalization logic across Carousel, SearchGrid, and OrderItem structures
- Persisted GraphQL operations: getBuyItAgainCarousel & ModifiableOrderDetailsRequest
- SSR __NEXT_DATA__ parsing for /my-account/your-orders and buy-it-again
- Error boundaries: loud failure on unauthenticated session and WAF interception
- HEBClient delegation tests
"""

import json
import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path

from scripts.cdp_harvester import SessionState
from scripts.heb_graphql import (
    HEBGraphQLClient,
    HEBAuthenticationError,
    HEBWAFChallengeError,
    HEBGraphQLError,
    PERSISTED_QUERIES,
    fetch_buy_it_again_items,
    fetch_past_orders,
    fetch_order_details,
    fetch_past_order_items,
)
from scripts.heb_client import HEBClient


class TestPastOrdersAndBuyItAgain(unittest.TestCase):
    """Test suite for past orders, order details, and Buy It Again features."""

    def setUp(self):
        self.client = HEBGraphQLClient(store_id="639")
        self.mock_auth_session = SessionState(
            source="test_auth",
            cookies={"sat": "mock.jwt.token", "reese84": "mock_waf_token", "sst": "mock_refresh"},
            raw_cookies=[{"name": "sat", "value": "mock.jwt.token"}],
            is_authenticated=True,
            has_sat=True,
            has_reese84=True,
            user_agent="Mozilla/5.0 TestAgent",
        )

    def test_persisted_query_hashes_registered(self):
        """Ensure getBuyItAgainCarousel and ModifiableOrderDetailsRequest hashes are in registry."""
        self.assertIn("getBuyItAgainCarousel", PERSISTED_QUERIES)
        self.assertEqual(
            PERSISTED_QUERIES["getBuyItAgainCarousel"],
            "9b25761ca9a712193aa89103789bf34160a3f24546bc8c6ce5ac3367026ce18e",
        )
        self.assertIn("ModifiableOrderDetailsRequest", PERSISTED_QUERIES)
        self.assertEqual(
            PERSISTED_QUERIES["ModifiableOrderDetailsRequest"],
            "c8e45777b79e4a2a8480b21e921c0c3441e8570b767a9f7af5d71041421a872e",
        )

    def test_normalize_carousel_item(self):
        """Verify normalization of a raw carousel Product item."""
        raw_carousel_item = {
            "id": "1850806",
            "fullDisplayName": "H-E-B Bakery Mini Butter Croissants",
            "fullCategoryHierarchy": "Bakery & bread/Desserts & pastries/Croissants",
            "pastPurchaseInfo": {
                "lastPurchaseDate": "2026-10-01T13:54:45.000Z",
            },
            "brand": {
                "name": "H-E-B",
            },
            "productCategory": {
                "name": "Bakery & bread",
            },
            "SKUs": [
                {
                    "id": "23211100000",
                    "customerFriendlySize": "8 ct",
                    "contextPrices": [
                        {
                            "context": "CURBSIDE",
                            "salePrice": {"amount": 5.23},
                            "listPrice": {"amount": 6.28},
                        }
                    ],
                }
            ],
        }

        normalized = self.client._normalize_product_item(raw_carousel_item)
        self.assertEqual(normalized["product_id"], "1850806")
        self.assertEqual(normalized["sku_id"], "23211100000")
        self.assertEqual(normalized["display_name"], "H-E-B Bakery Mini Butter Croissants")
        self.assertEqual(normalized["brand"], "H-E-B")
        self.assertEqual(normalized["package_size"], "8 ct")
        self.assertEqual(normalized["price"], 5.23)
        self.assertEqual(normalized["last_purchased"], "2026-10-01T13:54:45.000Z")
        self.assertEqual(normalized["purchase_count"], 1)
        self.assertEqual(normalized["category"], "Bakery & bread")
        self.assertEqual(normalized["department"], "Bakery & bread")

    def test_normalize_order_line_item(self):
        """Verify normalization of an order line item from ModifiableOrderDetailsRequest."""
        raw_order_item = {
            "fulfilledQuantity": 2,
            "quantity": 2,
            "totalUnitPrice": {
                "amount": 5.75,
            },
            "product": {
                "id": "11554014",
                "fullDisplayName": "Yasso Cookies 'n Cream Frozen Greek Yogurt Sandwiches",
                "productCategory": {
                    "name": "Frozen food",
                },
                "fullCategoryHierarchy": "Frozen food/Ice cream",
                "SKUs": [
                    {
                        "id": "85000197465",
                        "customerFriendlySize": "3 ct",
                    }
                ],
            },
        }

        normalized = self.client._normalize_product_item(
            raw_order_item, default_last_purchased="2026-10-12T17:30:00Z"
        )
        self.assertEqual(normalized["product_id"], "11554014")
        self.assertEqual(normalized["sku_id"], "85000197465")
        self.assertEqual(normalized["display_name"], "Yasso Cookies 'n Cream Frozen Greek Yogurt Sandwiches")
        self.assertEqual(normalized["brand"], "Yasso")
        self.assertEqual(normalized["package_size"], "3 ct")
        self.assertEqual(normalized["price"], 5.75)
        self.assertEqual(normalized["last_purchased"], "2026-10-12T17:30:00Z")
        self.assertEqual(normalized["purchase_count"], 2)
        self.assertEqual(normalized["category"], "Frozen food")
        self.assertEqual(normalized["department"], "Frozen food")

    def test_normalize_brand_detection_fallbacks(self):
        """Verify brand detection heuristics when brand object is not present."""
        item1 = {
            "id": "1001",
            "fullDisplayName": "Hill Country Fare Large Grade A Eggs",
            "SKUs": [{"id": "1001-sku", "customerFriendlySize": "12 ct"}],
        }
        norm1 = self.client._normalize_product_item(item1)
        self.assertEqual(norm1["brand"], "Hill Country Fare")

        item2 = {
            "id": "1002",
            "fullDisplayName": "Central Market Organic Extra Virgin Olive Oil",
            "SKUs": [{"id": "1002-sku", "customerFriendlySize": "16.9 oz"}],
        }
        norm2 = self.client._normalize_product_item(item2)
        self.assertEqual(norm2["brand"], "Central Market")

    def test_unauthenticated_session_fails_loudly(self):
        """Ensure operations raise HEBAuthenticationError loudly when session is unauthenticated."""
        unauth_session = SessionState(
            source="none",
            cookies={},
            is_authenticated=False,
            has_sat=False,
        )
        with patch.object(self.client, "get_session", return_value=unauth_session):
            with self.assertRaises(HEBAuthenticationError):
                self.client.fetch_buy_it_again_items()

            with self.assertRaises(HEBAuthenticationError):
                self.client.fetch_past_orders()

            with self.assertRaises(HEBAuthenticationError):
                self.client.fetch_order_details("HEB12345")

            with self.assertRaises(HEBAuthenticationError):
                self.client.fetch_past_order_items("HEB12345")

    def test_fetch_order_details_invalid_id(self):
        """Ensure empty or whitespace order_id raises ValueError."""
        with self.assertRaises(ValueError):
            self.client.fetch_order_details("")
        with self.assertRaises(ValueError):
            self.client.fetch_order_details("   ")

    @patch.object(HEBGraphQLClient, "get_session")
    @patch.object(HEBGraphQLClient, "execute_persisted_query")
    def test_fetch_buy_it_again_items_graphql_success(self, mock_exec, mock_get_sess):
        """Test successful retrieval and normalization of Buy It Again carousel."""
        mock_get_sess.return_value = self.mock_auth_session
        mock_exec.return_value = {
            "getBuyItAgainCarousel": {
                "items": [
                    {
                        "id": "999",
                        "fullDisplayName": "H-E-B 2% Reduced Fat Milk",
                        "brand": {"name": "H-E-B"},
                        "SKUs": [{"id": "999-sku", "customerFriendlySize": "1 gal"}],
                        "productCategory": {"name": "Dairy & eggs"},
                    }
                ]
            }
        }

        items = self.client.fetch_buy_it_again_items(limit=1)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["product_id"], "999")
        self.assertEqual(items[0]["brand"], "H-E-B")
        self.assertEqual(items[0]["display_name"], "H-E-B 2% Reduced Fat Milk")
        mock_exec.assert_called_once()
        call_args = mock_exec.call_args[0]
        self.assertEqual(call_args[0], "getBuyItAgainCarousel")
        self.assertEqual(call_args[1]["userIsLoggedIn"], True)

    @patch.object(HEBGraphQLClient, "get_session")
    @patch.object(HEBGraphQLClient, "execute_persisted_query")
    def test_fetch_buy_it_again_items_augmented_with_ssr(self, mock_exec, mock_get_sess):
        """Test augmenting carousel with SSR items when limit exceeds carousel count."""
        mock_get_sess.return_value = self.mock_auth_session
        mock_exec.return_value = {
            "getBuyItAgainCarousel": {
                "items": [
                    {
                        "id": "101",
                        "fullDisplayName": "Carousel Item",
                        "SKUs": [{"id": "101-sku"}],
                    }
                ]
            }
        }

        sample_next_data = {
            "props": {
                "pageProps": {
                    "layout": {
                        "visualComponents": [
                            {
                                "type": "searchGridV2",
                                "items": [
                                    {
                                        "id": "101",  # duplicate, should be skipped
                                        "fullDisplayName": "Carousel Item",
                                        "SKUs": [{"id": "101-sku"}],
                                    },
                                    {
                                        "id": "102",  # new item
                                        "fullDisplayName": "SSR Grid Item",
                                        "SKUs": [{"id": "102-sku"}],
                                    },
                                ],
                            }
                        ]
                    }
                }
            }
        }
        mock_html = f'<html><body><script id="__NEXT_DATA__" type="application/json">{json.dumps(sample_next_data)}</script></body></html>'
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = mock_html

        with patch("curl_cffi.requests.get", return_value=mock_resp):
            items = self.client.fetch_buy_it_again_items(limit=2)
            self.assertEqual(len(items), 2)
            self.assertEqual(items[0]["product_id"], "101")
            self.assertEqual(items[1]["product_id"], "102")

    @patch.object(HEBGraphQLClient, "get_session")
    @patch.object(HEBGraphQLClient, "execute_persisted_query")
    def test_fetch_order_details_graphql_success(self, mock_exec, mock_get_sess):
        """Test fetching order details via ModifiableOrderDetailsRequest."""
        mock_get_sess.return_value = self.mock_auth_session
        mock_exec.return_value = {
            "orderDetailsRequest": {
                "order": {
                    "orderId": "HEB231138630619403264",
                    "status": "PAYMENT_RECEIPTED",
                    "orderItems": [{"quantity": 1}],
                }
            }
        }

        order = self.client.fetch_order_details("HEB231138630619403264")
        self.assertEqual(order["orderId"], "HEB231138630619403264")
        self.assertEqual(order["status"], "PAYMENT_RECEIPTED")
        mock_exec.assert_called_once()
        self.assertEqual(mock_exec.call_args[0][0], "ModifiableOrderDetailsRequest")
        self.assertEqual(mock_exec.call_args[0][1]["orderId"], "HEB231138630619403264")

    @patch.object(HEBGraphQLClient, "get_session")
    def test_fetch_past_orders_ssr_success(self, mock_get_sess):
        """Test parsing past orders from /my-account/your-orders __NEXT_DATA__."""
        mock_get_sess.return_value = self.mock_auth_session
        sample_next_data = {
            "props": {
                "pageProps": {
                    "orders": [
                        {
                            "orderId": "HEB231138630619403264",
                            "status": "PAYMENT_RECEIPTED",
                            "orderStatusMessageShort": "Delivered",
                            "fulfillmentMethod": "DELIVERY",
                            "store": {"name": "Mueller H-E-B"},
                            "orderTimeslot": {"startDateTime": "2026-10-12T17:30:00Z"},
                            "totalPrice": {"formattedAmount": "$142.99"},
                            "productCount": 23,
                        }
                    ]
                }
            }
        }
        mock_html = f'<html><body><script id="__NEXT_DATA__" type="application/json">{json.dumps(sample_next_data)}</script></body></html>'

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = mock_html

        with patch("curl_cffi.requests.get", return_value=mock_resp):
            orders = self.client.fetch_past_orders(page=1)
            self.assertEqual(len(orders), 1)
            self.assertEqual(orders[0]["order_id"], "HEB231138630619403264")
            self.assertEqual(orders[0]["status_message"], "Delivered")
            self.assertEqual(orders[0]["order_date"], "2026-10-12T17:30:00Z")
            self.assertEqual(orders[0]["total_price"], "$142.99")
            self.assertEqual(orders[0]["product_count"], 23)

    @patch.object(HEBGraphQLClient, "get_session")
    def test_fetch_past_orders_waf_challenge_raises(self, mock_get_sess):
        """Test that WAF challenge on past orders page raises HEBWAFChallengeError."""
        mock_get_sess.return_value = self.mock_auth_session
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.text = "Pardon Our Interruption"

        with patch("curl_cffi.requests.get", return_value=mock_resp):
            with self.assertRaises(HEBWAFChallengeError):
                self.client.fetch_past_orders()

    @patch.object(HEBGraphQLClient, "get_session")
    @patch.object(HEBGraphQLClient, "fetch_order_details")
    @patch.object(HEBGraphQLClient, "fetch_past_orders")
    def test_fetch_past_order_items_aggregate(self, mock_past_orders, mock_order_details, mock_get_sess):
        """Test aggregating items across past orders up to limit."""
        mock_get_sess.return_value = self.mock_auth_session
        mock_past_orders.return_value = [
            {"order_id": "ORD1", "order_date": "2026-10-12T17:30:00Z"},
            {"order_id": "ORD2", "order_date": "2026-10-01T14:00:00Z"},
        ]
        mock_order_details.side_effect = [
            {
                "orderItems": [
                    {
                        "fulfilledQuantity": 1,
                        "product": {
                            "id": "100",
                            "fullDisplayName": "Item 1",
                            "SKUs": [{"id": "s1", "customerFriendlySize": "1 ct"}],
                        },
                    },
                    {
                        "fulfilledQuantity": 2,
                        "product": {
                            "id": "101",
                            "fullDisplayName": "Item 2",
                            "SKUs": [{"id": "s2", "customerFriendlySize": "2 ct"}],
                        },
                    },
                ]
            },
            {
                "orderItems": [
                    {
                        "fulfilledQuantity": 3,
                        "product": {
                            "id": "100",  # Duplicate item, should be skipped
                            "fullDisplayName": "Item 1",
                            "SKUs": [{"id": "s1", "customerFriendlySize": "1 ct"}],
                        },
                    },
                    {
                        "fulfilledQuantity": 1,
                        "product": {
                            "id": "102",
                            "fullDisplayName": "Item 3",
                            "SKUs": [{"id": "s3", "customerFriendlySize": "3 ct"}],
                        },
                    },
                ]
            },
        ]

        items = self.client.fetch_past_order_items(limit=3)
        self.assertEqual(len(items), 3)
        self.assertEqual([i["product_id"] for i in items], ["100", "101", "102"])
        self.assertEqual(items[0]["last_purchased"], "2026-10-12T17:30:00Z")
        self.assertEqual(items[2]["last_purchased"], "2026-10-01T14:00:00Z")

    def test_heb_client_delegation(self):
        """Test that HEBClient delegates past order methods to graphql_client."""
        heb_client = HEBClient(store_id="639")
        with patch.object(heb_client.graphql_client, "fetch_buy_it_again_items", return_value=[{"product_id": "1"}]) as m_bia, \
             patch.object(heb_client.graphql_client, "fetch_past_orders", return_value=[{"order_id": "2"}]) as m_po, \
             patch.object(heb_client.graphql_client, "fetch_order_details", return_value={"orderId": "3"}) as m_od, \
             patch.object(heb_client.graphql_client, "fetch_past_order_items", return_value=[{"product_id": "4"}]) as m_poi:

            res_bia = heb_client.fetch_buy_it_again_items(limit=5)
            self.assertEqual(res_bia, [{"product_id": "1"}])
            m_bia.assert_called_once_with(store_id="639", limit=5)

            res_po = heb_client.fetch_past_orders(page=2)
            self.assertEqual(res_po, [{"order_id": "2"}])
            m_po.assert_called_once_with(page=2)

            res_od = heb_client.fetch_order_details("HEB123")
            self.assertEqual(res_od, {"orderId": "3"})
            m_od.assert_called_once_with(order_id="HEB123")

            res_poi = heb_client.fetch_past_order_items(order_id="HEB123", limit=10)
            self.assertEqual(res_poi, [{"product_id": "4"}])
            m_poi.assert_called_once_with(order_id="HEB123", limit=10)


if __name__ == "__main__":
    unittest.main()
