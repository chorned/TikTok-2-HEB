"""
tests/test_heb_client.py

Comprehensive unit tests for Tier-0 Preferred SKU Resolution, package size
scaling mathematics, ad-hoc ingredient staging, and OOS cascade integration
in scripts/heb_client.py.
"""

import os
import sys
import unittest
import tempfile
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.cache_manager import ScaledIngredient, HEBCartItem
from scripts.heb_client import (
    HEBClient,
    parse_quantity_and_unit,
    calculate_package_quantity,
    extract_unit_standard,
    find_preferred_sku,
    match_ingredient_to_sku,
    stage_ingredient,
    HEBCheckoutGateError,
)


class TestHEBClientPackageMath(unittest.TestCase):
    """Tests quantity and unit parsing, unit conversion, and package size math."""

    def test_parse_quantity_and_unit(self):
        # 1. Simple integers and floats
        val, unit = parse_quantity_and_unit("48 oz beef broth")
        self.assertEqual(val, 48.0)
        self.assertEqual(unit, "oz")

        val, unit = parse_quantity_and_unit("1.25 lbs Atlantic salmon")
        self.assertEqual(val, 1.25)
        self.assertEqual(unit, "lb")

        # 2. Fractions
        val, unit = parse_quantity_and_unit("1/2 gal whole milk")
        self.assertEqual(val, 0.5)
        self.assertEqual(unit, "gal")

        val, unit = parse_quantity_and_unit("1 1/2 lbs ground beef")
        self.assertEqual(val, 1.5)
        self.assertEqual(unit, "lb")

        # 3. Counts and items
        val, unit = parse_quantity_and_unit("12 ct eggs")
        self.assertEqual(val, 12.0)
        self.assertEqual(unit, "ct")

        val, unit = parse_quantity_and_unit("6 eggs")
        self.assertEqual(val, 6.0)
        self.assertEqual(unit, "ct")

        val, unit = parse_quantity_and_unit("2 avocados")
        self.assertEqual(val, 2.0)
        self.assertEqual(unit, "ct")

        # 4. Volume units
        val, unit = parse_quantity_and_unit("6 cups chicken broth")
        self.assertEqual(val, 6.0)
        self.assertEqual(unit, "cup")

        # 5. Invalid / empty
        val, unit = parse_quantity_and_unit("")
        self.assertIsNone(val)
        self.assertIsNone(unit)

    def test_calculate_package_quantity_volume(self):
        # 48 oz recipe requirement vs 32 oz package -> 2 packages (Refinement #2)
        qty = calculate_package_quantity(
            requirement_str="48 oz beef broth",
            package_size_str="32 oz",
        )
        self.assertEqual(qty, 2)

        # 32 oz requirement vs 32 oz package -> 1 package
        qty = calculate_package_quantity(
            requirement_str="32 oz beef broth",
            package_size_str="32 oz",
        )
        self.assertEqual(qty, 1)

        # 16 oz requirement vs 32 oz package -> 1 package (minimum 1)
        qty = calculate_package_quantity(
            requirement_str="16 oz beef broth",
            package_size_str="32 oz",
        )
        self.assertEqual(qty, 1)

        # 6 cups (48 fl oz) vs 32 oz package -> 2 packages
        qty = calculate_package_quantity(
            requirement_str="6 cups chicken broth",
            package_size_str="32 oz",
        )
        self.assertEqual(qty, 2)

        # 1 gallon (128 fl oz) vs 1/2 gal (64 fl oz) -> 2 packages
        qty = calculate_package_quantity(
            requirement_str="1 gal whole milk",
            package_size_str="1/2 gal",
        )
        self.assertEqual(qty, 2)

    def test_calculate_package_quantity_weight(self):
        # 2 lbs ground beef vs 16 oz (1 lb) package -> 2 packages
        qty = calculate_package_quantity(
            requirement_str="2 lbs ground beef",
            package_size_str="16 oz",
        )
        self.assertEqual(qty, 2)

        # 1.5 lbs salmon fillet vs 1.25 lb tray -> 2 packages
        qty = calculate_package_quantity(
            requirement_str="1.5 lbs salmon",
            package_size_str="1.25 lb",
        )
        self.assertEqual(qty, 2)

    def test_calculate_package_quantity_count(self):
        # 18 eggs vs 12 ct carton -> 2 cartons
        qty = calculate_package_quantity(
            requirement_str="18 eggs",
            package_size_str="12 ct",
        )
        self.assertEqual(qty, 2)

        # 6 eggs vs 12 ct carton -> 1 carton
        qty = calculate_package_quantity(
            requirement_str="6 eggs",
            package_size_str="12 ct",
        )
        self.assertEqual(qty, 1)


class TestHEBClientTier0Resolution(unittest.TestCase):
    """Tests Tier-0 Preferred SKU resolution and fallback cascade in HEBClient."""

    def setUp(self):
        self.sample_preferred = {
            "beef broth": {
                "canonical_key": "beef broth",
                "aliases": ["beef broth", "beef stock"],
                "product_id": "999123",
                "sku_id": "999123001",
                "display_name": "H-E-B Beef Broth",
                "brand": "H-E-B",
                "package_size": "32 oz",
                "unit_size": 32,
                "unit": "oz",
                "source": "past_orders",
                "effective_weight": 3.0,
                "order_count": 1,
            },
            "whole milk": {
                "canonical_key": "whole milk",
                "aliases": ["milk", "whole milk"],
                "product_id": "777888",
                "sku_id": "777888001",
                "display_name": "H-E-B Organics Whole Milk",
                "brand": "H-E-B Organics",
                "package_size": "1 gal",
                "unit_size": 1,
                "unit": "gal",
                "source": "manual_override",
                "effective_weight": 999.0,
                "order_count": 1,
            },
        }
        self.client = HEBClient(store_id="639")

    def test_find_preferred_sku_matching(self):
        # Direct key match
        match = self.client.find_preferred_sku("whole milk", preferred_map=self.sample_preferred)
        self.assertIsNotNone(match)
        self.assertEqual(match["product_id"], "777888")

        # Recipe requirement phrase match
        match = self.client.find_preferred_sku("48 oz beef broth", preferred_map=self.sample_preferred)
        self.assertIsNotNone(match)
        self.assertEqual(match["product_id"], "999123")

        # Alias match
        match = self.client.find_preferred_sku("beef stock", preferred_map=self.sample_preferred)
        self.assertIsNotNone(match)
        self.assertEqual(match["product_id"], "999123")

        # Non-existent item
        match = self.client.find_preferred_sku("dragonfruit", preferred_map=self.sample_preferred)
        self.assertIsNone(match)

    def test_tier_0_match_with_package_size_math_in_stock(self):
        # Candidate pool contains preferred beef broth in stock
        candidates = [
            {
                "product_id": "999123",
                "sku_id": "999123001",
                "name": "H-E-B Beef Broth, 32 oz",
                "brand": "H-E-B",
                "package_size": "32 oz",
                "price": 2.48,
                "in_stock": True,
            },
            {
                "product_id": "111222",
                "sku_id": "111222001",
                "name": "Swanson Beef Broth, 32 oz",
                "brand": "Swanson",
                "package_size": "32 oz",
                "price": 2.98,
                "in_stock": True,
            },
        ]

        ingredient = ScaledIngredient(
            ingredient_key="beef_broth",
            recipe_requirement="48 oz beef broth",
            heb_search_query="beef broth",
        )

        match = self.client.match_ingredient_to_sku(
            ingredient=ingredient,
            candidate_pool=candidates,
            preferred_skus_cache=self.sample_preferred,
        )

        self.assertEqual(match["tier"], 0)
        self.assertEqual(match["status"], "SUCCESS")
        self.assertEqual(match["sku"]["heb_product_id"], "999123")
        # 48 oz requirement / 32 oz package size = 2 packages
        self.assertEqual(match["sku"]["quantity_to_order"], 2)
        self.assertFalse(match["needs_human_call"])

    def test_tier_0_out_of_stock_smooth_fallback_to_tier_1_or_2(self):
        # Preferred beef broth is marked out of stock (in_stock=False)
        candidates = [
            {
                "product_id": "999123",
                "sku_id": "999123001",
                "name": "H-E-B Beef Broth, 32 oz",
                "brand": "H-E-B",
                "package_size": "32 oz",
                "price": 2.48,
                "in_stock": False,  # OOS!
            },
            {
                "product_id": "111222",
                "sku_id": "111222001",
                "name": "Central Market Organic Beef Broth, 32 oz",
                "brand": "Central Market",
                "package_size": "32 oz",
                "price": 3.48,
                "in_stock": True,
            },
        ]

        ingredient = ScaledIngredient(
            ingredient_key="beef_broth",
            recipe_requirement="48 oz beef broth",
            heb_search_query="beef broth",
        )

        match = self.client.match_ingredient_to_sku(
            ingredient=ingredient,
            candidate_pool=candidates,
            preferred_skus_cache=self.sample_preferred,
        )

        # Smooth fallback to Tier 2 (brand hierarchy substitution)
        self.assertEqual(match["tier"], 2)
        self.assertEqual(match["status"], "SUBSTITUTED")
        self.assertEqual(match["sku"]["heb_product_id"], "111222")

    def test_adhoc_ingredient_string_resolution(self):
        # Passing plain string "48 oz beef broth" directly
        candidates = [
            {
                "product_id": "999123",
                "name": "H-E-B Beef Broth",
                "brand": "H-E-B",
                "package_size": "32 oz",
                "price": 2.48,
                "in_stock": True,
            }
        ]

        match = self.client.match_ingredient_to_sku(
            ingredient="48 oz beef broth",
            candidate_pool=candidates,
            preferred_skus_cache=self.sample_preferred,
        )

        self.assertEqual(match["tier"], 0)
        self.assertEqual(match["sku"]["heb_product_id"], "999123")
        self.assertEqual(match["sku"]["quantity_to_order"], 2)

    def test_stage_ingredient_checkout_gate(self):
        # Calling stage_ingredient without confirm=True returns a safe preview dict
        candidates = [
            {
                "product_id": "999123",
                "name": "H-E-B Beef Broth",
                "brand": "H-E-B",
                "package_size": "32 oz",
                "price": 2.48,
                "in_stock": True,
            }
        ]

        with patch.object(self.client, "search_heb_product", return_value=candidates):
            res = self.client.stage_ingredient(
                ingredient="48 oz beef broth",
                confirm=False,  # HITL Gate enforced
                preferred_skus_cache=self.sample_preferred,
            )
            self.assertIn("cart_response", res)
            self.assertTrue(res["cart_response"]["preview"])
            self.assertEqual(res["cart_response"]["action"], "cart_add")
            self.assertEqual(res["cart_response"]["product_id"], "999123")
            self.assertEqual(res["cart_response"]["quantity"], 2)

    def test_tier_0_manual_override_priority(self):
        # When two preferred SKUs match the query, manual_override wins over past_orders
        dual_preferred = {
            "milk": {
                "canonical_key": "milk",
                "aliases": ["milk"],
                "product_id": "111111",
                "display_name": "Store Brand Regular Milk",
                "brand": "H-E-B",
                "package_size": "1 gal",
                "source": "past_orders",
                "effective_weight": 6.0,
            },
            "whole milk": {
                "canonical_key": "whole milk",
                "aliases": ["milk", "whole milk"],
                "product_id": "222222",
                "display_name": "Pinned Organic Whole Milk",
                "brand": "H-E-B Organics",
                "package_size": "1 gal",
                "source": "manual_override",
                "effective_weight": 999.0,
            }
        }
        matched = self.client.find_preferred_sku("whole milk", preferred_map=dual_preferred)
        self.assertEqual(matched["product_id"], "222222")
        self.assertEqual(matched["source"], "manual_override")

    def test_stage_ingredient_confirmed(self):
        candidates = [
            {
                "product_id": "999123",
                "sku_id": "999123001",
                "name": "H-E-B Beef Broth",
                "brand": "H-E-B",
                "package_size": "32 oz",
                "price": 2.48,
                "in_stock": True,
            }
        ]

        with patch.object(self.client, "search_heb_product", return_value=candidates):
            with patch.object(self.client, "cart_add", return_value={"ok": True, "count": 1}) as mock_add:
                res = self.client.stage_ingredient(
                    ingredient="48 oz beef broth",
                    confirm=True,
                    preferred_skus_cache=self.sample_preferred,
                )
                self.assertEqual(res["match"]["tier"], 0)
                mock_add.assert_called_once_with(
                    product_id="999123",
                    sku_id="999123001",
                    quantity=2,
                    confirm=True,
                )
                self.assertEqual(res["cart_response"], {"ok": True, "count": 1})


if __name__ == "__main__":
    unittest.main()
