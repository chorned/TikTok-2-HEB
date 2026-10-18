"""
tests/test_preferred_skus.py

Unit tests for H-E-B Preferred SKUs Engine (CHO-245):
- Canonical culinary alias normalization & robust heuristic extraction
- Non-food household item filtering (cleaning, paper goods, pet, personal care)
- Time-decay weighting & smart brand-switch vs staple thresholds
- Manual override pin priority and --force behavior
- Cache storage schema compliance (cache/preferred_skus.json)
- Recipe promote-sku integration and CLI subcommand verification
"""

import os
import sys
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.cache_manager import (
    CacheManager,
    PreferredSKU,
    PreferredSKUsCache,
    is_food_item,
    extract_culinary_info,
    parse_package_size,
    calculate_effective_weight,
    update_preferred_skus,
    pin_preferred_sku,
)
from scripts.heb_graphql import HEBAuthenticationError, HEBWAFChallengeError


class TestPreferredSKUsEngine(unittest.TestCase):
    """Test suite for Preferred SKUs normalization, decay, and caching."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.cache_dir = Path(self.test_dir) / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.mgr = CacheManager(cache_dir=str(self.cache_dir))

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    # ==========================================================================
    # 1. Culinary Alias Key Normalization & Non-Food Filtering
    # ==========================================================================

    def test_canonical_culinary_extraction_staples(self):
        """Test extraction of canonical key and aliases from verbose H-E-B titles."""
        cases = [
            (
                "Central Market Organics Grade A Large Brown Eggs, 12 ct",
                "eggs",
                ["eggs", "large eggs", "brown eggs", "egg"],
            ),
            (
                "H-E-B Reduced Fat 2% Milk, 1/2 gal",
                "2% milk",
                ["2% milk", "reduced fat milk", "milk"],
            ),
            (
                "Fresh Bunch of Organic Bananas, 4-7 Bananas",
                "bananas",
                ["bananas", "organic bananas", "banana"],
            ),
            (
                "H-E-B Bakery Mini Butter Croissants, 8 ct",
                "croissants",
                ["croissants", "mini butter croissants", "butter croissants"],
            ),
            (
                "H-E-B 100% Pure Ground Beef Chuck, 80% Lean, 1 lb",
                "ground beef",
                ["ground beef", "ground beef chuck", "ground chuck"],
            ),
            (
                "H-E-B Sharp Cheddar Cheese, 16 oz",
                "cheddar cheese",
                ["cheddar cheese", "sharp cheddar cheese", "cheddar"],
            ),
            (
                "Rustic Bakery Everything Spice Organic Sourdough Flatbread, 6 oz",
                "sourdough flatbread",
                ["sourdough flatbread", "flatbread"],
            ),
            (
                "Kerrygold Pure Irish Butter, Salted, 8 oz",
                "butter",
                ["butter", "salted butter", "unsalted butter"],
            ),
            (
                "H-E-B Bakery Butter Flour Tortillas, 10 ct",
                "tortillas",
                ["tortillas", "flour tortillas"],
            ),
        ]

        for product_name, expected_key, expected_aliases_subset in cases:
            key, aliases = extract_culinary_info(product_name)
            self.assertEqual(
                key,
                expected_key,
                f"Failed for '{product_name}': expected key '{expected_key}', got '{key}'",
            )
            for ea in expected_aliases_subset:
                self.assertIn(
                    ea,
                    aliases,
                    f"Alias '{ea}' missing from {aliases} for '{product_name}'",
                )

    def test_non_food_filtering(self):
        """Verify non-food household items are rejected while food items pass."""
        food_items = [
            {"display_name": "H-E-B Whole Milk, 1 gal", "category": "Dairy & eggs", "department": "Dairy & eggs"},
            {"display_name": "Fresh Organic Gala Apples", "category": "Fruit & vegetables", "department": "Produce"},
            {"display_name": "H-E-B Boneless Skinless Chicken Breasts", "category": "Meat & seafood", "department": "Meat"},
            {"display_name": "Chobani Plain Non-Fat Greek Yogurt", "category": "Dairy & eggs", "department": "Dairy"},
        ]

        non_food_items = [
            {"display_name": "Bounty Select-A-Size Paper Towels, 6 Double Rolls", "category": "Household", "department": "Paper goods"},
            {"display_name": "Charmin Ultra Soft Toilet Paper, 12 Mega Rolls", "category": "Household", "department": "Paper goods"},
            {"display_name": "Glad ForceFlex Tall Kitchen Trash Bags, 40 ct", "category": "Cleaning", "department": "Household"},
            {"display_name": "Dawn Ultra Liquid Dish Soap, 28 oz", "category": "Cleaning", "department": "Household"},
            {"display_name": "Tide PODS Free & Gentle Laundry Detergent, 42 ct", "category": "Cleaning", "department": "Laundry"},
            {"display_name": "Purina ONE SmartBlend Dry Dog Food, 15 lb", "category": "Pet", "department": "Pet care"},
            {"display_name": "Colgate Total Toothpaste, 4.8 oz", "category": "Beauty", "department": "Personal care"},
            {"display_name": "Clorox Disinfecting Wipes, 75 ct", "category": "Cleaning", "department": "Household"},
            {"display_name": "Pampers Baby Dry Diapers, Size 4", "category": "Baby", "department": "Baby care"},
        ]

        for fi in food_items:
            self.assertTrue(is_food_item(fi), f"Expected food item to pass: {fi['display_name']}")

        for nfi in non_food_items:
            self.assertFalse(is_food_item(nfi), f"Expected non-food item to be rejected: {nfi['display_name']}")

    def test_package_size_parsing(self):
        """Verify unit_size and unit extraction from various package size formats."""
        cases = [
            ("12 ct", 12, "ct"),
            ("1/2 gal", 0.5, "gal"),
            ("16 oz", 16, "oz"),
            ("1 lb", 1, "lb"),
            ("Avg. 2.425 lbs", 2.425, "lb"),
            ("8 ct", 8, "ct"),
            ("9 fl oz", 9, "fl oz"),
            ("57 oz", 57, "oz"),
            (None, None, None),
        ]
        for pkg_str, exp_val, exp_unit in cases:
            val, unit = parse_package_size(pkg_str)
            self.assertEqual(val, exp_val)
            self.assertEqual(unit, exp_unit)

    # ==========================================================================
    # 2. Time-Decay Weighting & Brand Switch vs Staple Thresholds
    # ==========================================================================

    def test_time_decay_brand_switch_over_stale_count(self):
        """
        Verify Refinement #3: 2 recent purchases in the last 30 days beat
        5 stale purchases from 4 months ago (brand switch inside My H-E-B app).
        """
        ref_time = datetime(2026, 10, 17, 12, 0, 0, tzinfo=timezone.utc)
        # Recent purchase: 14 days ago, count = 2
        recent_date = (ref_time - timedelta(days=14)).isoformat()
        weight_recent = calculate_effective_weight(order_count=2, last_purchased=recent_date, ref_time=ref_time)
        self.assertEqual(weight_recent, 6.0)  # 2 * 3.0 = 6.0

        # Stale purchase: 120 days ago, count = 5
        stale_date = (ref_time - timedelta(days=120)).isoformat()
        weight_stale = calculate_effective_weight(order_count=5, last_purchased=stale_date, ref_time=ref_time)
        self.assertLess(weight_stale, weight_recent)
        self.assertTrue(weight_recent > weight_stale, f"Recent switch ({weight_recent}) should beat stale ({weight_stale})")

    def test_time_decay_staple_beats_one_off_specialty(self):
        """
        Verify Refinement #3: A high-frequency staple (10 purchases, last bought 45 days ago)
        is NOT overwritten by a 1-off specialty purchase (1 purchase, 5 days ago).
        """
        ref_time = datetime(2026, 10, 17, 12, 0, 0, tzinfo=timezone.utc)
        # 1-off specialty purchase 5 days ago
        one_off_date = (ref_time - timedelta(days=5)).isoformat()
        weight_one_off = calculate_effective_weight(order_count=1, last_purchased=one_off_date, ref_time=ref_time)
        self.assertEqual(weight_one_off, 3.0)  # 1 * 3.0 = 3.0

        # High-frequency staple: 10 orders, last purchased 45 days ago
        staple_date = (ref_time - timedelta(days=45)).isoformat()
        weight_staple = calculate_effective_weight(order_count=10, last_purchased=staple_date, ref_time=ref_time)
        self.assertGreater(weight_staple, weight_one_off)
        self.assertTrue(weight_staple > weight_one_off, f"Staple ({weight_staple}) should beat 1-off ({weight_one_off})")

    # ==========================================================================
    # 3. Manual Override Protection & Pinning
    # ==========================================================================

    def test_pin_preferred_sku_creates_manual_override(self):
        """Verify pin_preferred_sku writes source='manual_override' and effective_weight=999.0."""
        entry = self.mgr.pin_preferred_sku(
            canonical_key="whole milk",
            product_id="999001",
            sku_id="999001-sku",
            name="H-E-B Organics Whole Milk",
            brand="H-E-B Organics",
            package_size="1 gal",
        )
        self.assertEqual(entry["canonical_key"], "whole milk")
        self.assertEqual(entry["source"], "manual_override")
        self.assertEqual(entry["effective_weight"], 999.0)
        self.assertEqual(entry["unit_size"], 1)
        self.assertEqual(entry["unit"], "gal")

        # Verify disk persistence
        data = self.mgr._load_preferred_skus()
        self.assertIn("whole milk", data["preferred_skus"])
        self.assertEqual(data["preferred_skus"]["whole milk"]["product_id"], "999001")

    def test_manual_override_protected_from_sync_unless_forced(self):
        """Verify sync does not overwrite manual_override unless force=True."""
        # 1. Pin manual override
        self.mgr.pin_preferred_sku(
            canonical_key="eggs",
            product_id="PINNED_EGG_ID",
            sku_id="PINNED_SKU",
            name="Vital Farms Pasture-Raised Large Eggs",
            brand="Vital Farms",
            package_size="12 ct",
        )

        # 2. Mock client returning competing past order item
        mock_client = MagicMock()
        mock_client.fetch_buy_it_again_items.return_value = [
            {
                "product_id": "PAST_ORDER_EGG_ID",
                "sku_id": "PAST_SKU",
                "display_name": "H-E-B Large Grade A Eggs, 12 ct",
                "brand": "H-E-B",
                "package_size": "12 ct",
                "last_purchased": datetime.now(timezone.utc).isoformat(),
                "purchase_count": 20,
                "category": "Dairy & eggs",
                "department": "Dairy & eggs",
            }
        ]
        mock_client.fetch_past_order_items.return_value = []

        # 3. Run sync without force -> Pinned override must remain
        res = self.mgr.update_preferred_skus(force=False, client=mock_client)
        egg_entry = res["preferred_skus"]["eggs"]
        self.assertEqual(egg_entry["product_id"], "PINNED_EGG_ID")
        self.assertEqual(egg_entry["source"], "manual_override")

        # 4. Run sync with force=True -> Overwrite is permitted
        res_forced = self.mgr.update_preferred_skus(force=True, client=mock_client)
        egg_entry_forced = res_forced["preferred_skus"]["eggs"]
        self.assertEqual(egg_entry_forced["product_id"], "PAST_ORDER_EGG_ID")
        self.assertEqual(egg_entry_forced["source"], "past_orders")

    # ==========================================================================
    # 4. Promote SKU Integration
    # ==========================================================================

    def test_promote_sku_updates_recipe_and_pins_preferred_sku(self):
        """Verify promote_sku updates recipe YAML and pins the SKU in cache/preferred_skus.json."""
        # Setup sample recipe
        recipe_data = {
            "id": "creamy-pasta",
            "title": "Creamy Pasta",
            "source_url": "https://tiktok.com/@chef/video/123",
            "video_id": "123",
            "ingredients_to_buy": [
                {
                    "ingredient_key": "heavy cream",
                    "recipe_requirement": "1 cup heavy cream",
                    "heb_search_query": "heavy cream",
                    "primary_sku": {
                        "heb_product_id": "OLD_CREAM_ID",
                        "heb_product_name": "Old Brand Heavy Cream",
                    },
                    "fallback_skus": [],
                }
            ],
        }
        self.mgr.save(recipe_data)

        # Promote new SKU
        success = self.mgr.promote_sku(
            recipe_id="creamy-pasta",
            ingredient_key="heavy cream",
            product_id="NEW_CREAM_ID",
            name="H-E-B Organic Heavy Whipping Cream",
            qty=1,
            brand="H-E-B Organics",
            package_size="16 oz",
        )
        self.assertTrue(success)

        # Verify recipe was updated
        _, updated_recipe = self.mgr.lookup("creamy-pasta")
        primary = updated_recipe["ingredients_to_buy"][0]["primary_sku"]
        self.assertEqual(primary["heb_product_id"], "NEW_CREAM_ID")
        self.assertEqual(primary["heb_product_name"], "H-E-B Organic Heavy Whipping Cream")

        # Verify preferred_skus.json was pinned
        data = self.mgr._load_preferred_skus()
        pinned = data["preferred_skus"].get("heavy cream")
        self.assertIsNotNone(pinned)
        self.assertEqual(pinned["product_id"], "NEW_CREAM_ID")
        self.assertEqual(pinned["source"], "manual_override")
        self.assertEqual(pinned["package_size"], "16 oz")

    # ==========================================================================
    # 5. Loud Failure at Auth / WAF Boundary
    # ==========================================================================

    def test_sync_fails_loudly_on_auth_error(self):
        """Ensure HEBAuthenticationError is raised loudly and not silently swallowed."""
        mock_client = MagicMock()
        mock_client.fetch_buy_it_again_items.side_effect = HEBAuthenticationError("Missing active session")

        with self.assertRaises(HEBAuthenticationError):
            self.mgr.update_preferred_skus(client=mock_client)

    def test_sync_fails_loudly_on_waf_challenge(self):
        """Ensure HEBWAFChallengeError is raised loudly and not silently swallowed."""
        mock_client = MagicMock()
        mock_client.fetch_buy_it_again_items.side_effect = HEBWAFChallengeError("Imperva WAF challenge 403")

        with self.assertRaises(HEBWAFChallengeError):
            self.mgr.update_preferred_skus(client=mock_client)


if __name__ == "__main__":
    unittest.main()
