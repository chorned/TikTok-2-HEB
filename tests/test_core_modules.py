"""
tests/test_core_modules.py

Unit test suite for heb-mcp.skill (TikTok-2-HEB) core modules:
- extract_recipe_media: Subtitle parsing & culinary token analysis
- cache_manager: Pydantic models, PantryComparator, HouseholdScaler, CacheManager CRUD
- heb_client: Catalog search, 4-tier OOS cascade engine
"""

import os
import sys
import shutil
import tempfile
import unittest
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from unittest.mock import patch
from scripts.extract_recipe_media import clean_subtitle_text, count_culinary_tokens
from scripts.cache_manager import (
    HEBCartItem,
    ScaledIngredient,
    RecipeCache,
    PantryComparator,
    HouseholdScaler,
    CacheManager,
)
from scripts.heb_client import (
    HEBClient,
    search_heb_product,
    match_ingredient_to_sku,
    HEBWAFChallengeError,
    HEBClientError,
)



class TestExtractRecipeMedia(unittest.TestCase):
    """Test media subtitle cleaning and token heuristics."""

    def test_clean_subtitle_text(self):
        raw_vtt = """WEBVTT
Kind: captions
Language: en

00:00:01.000 --> 00:00:03.500
<c>Cut</c> <c>salmon</c> into cubes

00:00:03.500 --> 00:00:06.000
<c>Cut</c> <c>salmon</c> into cubes
Add 2 tbsp cornstarch

00:00:06.000 --> 00:00:09.000
Fry in 1 tbsp sesame oil
"""
        cleaned = clean_subtitle_text(raw_vtt)
        self.assertIn("Cut salmon into cubes", cleaned)
        self.assertIn("Add 2 tbsp cornstarch", cleaned)
        self.assertIn("Fry in 1 tbsp sesame oil", cleaned)
        self.assertNotIn("WEBVTT", cleaned)
        self.assertNotIn("-->", cleaned)
        # Deduplication check: "Cut salmon into cubes" should not appear consecutively twice
        self.assertEqual(cleaned.count("Cut salmon into cubes"), 1)

    def test_count_culinary_tokens(self):
        sample = "Take 2 cups of jasmine rice, 1 tbsp of soy sauce, 1 tsp salt, and 4 cloves of garlic."
        count = count_culinary_tokens(sample)
        self.assertGreaterEqual(count, 4)

        empty_sample = "Just dancing to the music check out my fit!"
        self.assertEqual(count_culinary_tokens(empty_sample), 0)


class TestCacheManager(unittest.TestCase):
    """Test Pydantic models, pantry comparator, household scaler, and cache CRUD."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.cache_mgr = CacheManager(cache_dir=self.test_dir)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_pantry_comparator(self):
        comparator = PantryComparator()
        is_staple, item = comparator.check_staple("1 tsp kosher salt")
        self.assertTrue(is_staple)
        self.assertIsNotNone(item)
        self.assertIn("salt", item["name"].lower())

        is_staple2, item2 = comparator.check_staple("2 tbsp olive oil")
        self.assertTrue(is_staple2)

        is_staple3, item3 = comparator.check_staple("1.25 lbs fresh salmon fillet")
        self.assertFalse(is_staple3)

    def test_household_scaler_and_kid_split(self):
        scaler = HouseholdScaler()
        # Scale 4 servings to 2.5 servings
        factor = scaler.calculate_scale_factor(original_servings=4.0)
        self.assertAlmostEqual(factor, 0.65, places=2)

        # Spice detection
        self.assertTrue(scaler.is_kid_sensitive("1 tbsp sriracha"))
        self.assertTrue(scaler.is_kid_sensitive("1/2 tsp crushed red pepper"))
        self.assertFalse(scaler.is_kid_sensitive("1 tbsp honey"))

        # Kid adaptations generation
        adaptations = scaler.generate_kid_adaptations(
            ingredients=["1.25 lbs salmon fillet", "1 tbsp sriracha", "2 tbsp honey"],
            instructions=["Cook salmon", "Stir in sriracha and honey"]
        )
        self.assertTrue(any("sriracha" in a.lower() for a in adaptations))
        self.assertTrue(any("1/2-inch" in a.lower() for a in adaptations))

    def test_recipe_cache_save_and_lookup(self):
        recipe = RecipeCache(
            id="crispy-honey-garlic-salmon",
            title="Crispy Honey Garlic Salmon Bites",
            source_url="https://www.tiktok.com/@chef/video/123456789",
            video_id="123456789",
            platform="tiktok",
            tags=["seafood", "weeknight"],
            scaling={"target_servings": 2.5, "scale_factor": 0.65},
            ingredients_to_buy=[
                ScaledIngredient(
                    ingredient_key="salmon_fillet",
                    recipe_requirement="1.25 lbs salmon fillet",
                    heb_search_query="H-E-B Fresh Atlantic Salmon Fillet",
                    primary_sku=HEBCartItem(
                        heb_product_id="319482",
                        heb_product_name="H-E-B Fresh Atlantic Salmon Fillet",
                        package_size="~1.25 lb tray",
                        quantity_to_order=1,
                    ),
                )
            ],
            instructions=["Cut salmon", "Sear in pan", "Glaze with sauce"],
        )

        saved_path = self.cache_mgr.save(recipe)
        self.assertTrue(os.path.exists(saved_path))

        # Lookup by video_id
        status, hit = self.cache_mgr.lookup("123456789")
        self.assertEqual(status, "HIT")
        self.assertEqual(hit["title"], "Crispy Honey Garlic Salmon Bites")

        # Lookup by URL
        status_url, hit_url = self.cache_mgr.lookup("https://www.tiktok.com/@chef/video/123456789")
        self.assertEqual(status_url, "HIT")

        # Promote SKU
        promoted = self.cache_mgr.promote_sku(
            recipe_id="crispy-honey-garlic-salmon",
            ingredient_key="salmon_fillet",
            product_id="999999",
            name="Central Market Wild Sockeye Salmon",
            qty=1,
        )
        self.assertTrue(promoted)

        _, updated = self.cache_mgr.lookup("crispy-honey-garlic-salmon")
        self.assertEqual(
            updated["ingredients_to_buy"][0]["primary_sku"]["heb_product_id"],
            "999999",
        )
        # Verify old primary was preserved in fallbacks
        fallbacks = updated["ingredients_to_buy"][0]["fallback_skus"]
        self.assertTrue(any(f["heb_product_id"] == "319482" for f in fallbacks))


class TestHEBClientAndCascade(unittest.TestCase):
    """Test H-E-B search and 4-tier Out-of-Stock fallback cascade."""

    def setUp(self):
        self.client = HEBClient(store_id="390")

    def test_search_heb_product(self):
        sample_cards = [
            {"product_id": "590001", "name": "H-E-B Jasmine Rice", "brand": "H-E-B", "price": 4.48, "in_stock": True, "package_size": "16 oz"},
            {"product_id": "590002", "name": "H-E-B Organics Jasmine Rice", "brand": "H-E-B Organics", "price": 5.28, "in_stock": True, "package_size": "16 oz"},
            {"product_id": "590003", "name": "Central Market Jasmine Rice", "brand": "Central Market", "price": 5.99, "in_stock": True, "package_size": "1 lb"},
        ]
        with patch.object(self.client.graphql_client, "product_search", return_value=sample_cards):
            results = self.client.search_heb_product("jasmine rice", store_id="390")
            self.assertGreaterEqual(len(results), 3)
            self.assertTrue(any("jasmine rice" in r["name"].lower() for r in results))

    def test_search_heb_product_waf_loud_failure(self):
        """Verify that Imperva WAF challenges trigger a loud HEBWAFChallengeError."""
        with patch.object(self.client.graphql_client, "product_search", side_effect=HEBWAFChallengeError("Imperva Anti-Bot WAF challenge blocked search")), \
             patch.object(self.client, "_search_via_playwright", side_effect=HEBWAFChallengeError("Imperva Anti-Bot WAF challenge blocked search")):
            with self.assertRaises(HEBWAFChallengeError):
                self.client.search_heb_product("jasmine rice", store_id="390")

    def test_cascade_tier_1_exact_match(self):
        candidates = [
            {
                "product_id": "319482",
                "name": "H-E-B Fresh Atlantic Salmon Fillet",
                "brand": "H-E-B",
                "package_size": "~1.25 lb tray",
                "price": 11.98,
                "in_stock": True,
            }
        ]
        target_id = "319482"

        ingredient = ScaledIngredient(
            ingredient_key="salmon",
            recipe_requirement="1 lb fresh salmon",
            heb_search_query="Atlantic Salmon",
            primary_sku=HEBCartItem(
                heb_product_id=target_id,
                heb_product_name=candidates[0]["name"],
                quantity_to_order=1,
            ),
        )

        match = self.client.match_ingredient_to_sku(ingredient, candidate_pool=candidates)
        self.assertEqual(match["tier"], 1)
        self.assertEqual(match["status"], "SUCCESS")
        self.assertEqual(match["sku"]["heb_product_id"], target_id)

    def test_cascade_tier_2_brand_substitution(self):
        # Provide candidate pool where primary is missing, but H-E-B store brand is present
        candidates = [
            {"product_id": "1001", "name": "H-E-B Broccoli Florets", "brand": "H-E-B", "in_stock": True, "package_size": "12 oz"},
            {"product_id": "1002", "name": "National Brand Broccoli", "brand": "National Brand", "in_stock": True, "package_size": "12 oz"},
        ]
        ingredient = ScaledIngredient(
            ingredient_key="broccoli",
            recipe_requirement="1 bag broccoli florets",
            heb_search_query="Broccoli",
            primary_sku=HEBCartItem(
                heb_product_id="9999",  # OOS / not in candidates
                heb_product_name="Green Giant Broccoli",
                quantity_to_order=1,
            ),
        )

        match = self.client.match_ingredient_to_sku(ingredient, candidate_pool=candidates)
        self.assertEqual(match["tier"], 2)
        self.assertEqual(match["status"], "SUBSTITUTED")
        self.assertEqual(match["sku"]["heb_product_id"], "1001")

    def test_cascade_tier_3_organic_conventional_swap(self):
        # Organic was requested, but only conventional is in stock
        candidates = [
            {"product_id": "2001", "name": "H-E-B Organic Honey", "brand": "H-E-B Organics", "in_stock": False, "is_organic": True},
            {"product_id": "2002", "name": "H-E-B Clover Honey", "brand": "H-E-B", "in_stock": True, "is_organic": False},
        ]
        ingredient = {
            "ingredient_key": "honey",
            "recipe_requirement": "1 bottle organic honey",
            "heb_search_query": "Organic Honey",
        }

        match = self.client.match_ingredient_to_sku(ingredient, candidate_pool=candidates)
        self.assertEqual(match["tier"], 3)
        self.assertEqual(match["status"], "SUBSTITUTED")
        self.assertEqual(match["sku"]["heb_product_id"], "2002")

    def test_cascade_tier_4_category_wipeout(self):
        # All candidate products are out of stock
        candidates = [
            {"product_id": "3001", "name": "Dragonfruit", "in_stock": False},
            {"product_id": "3002", "name": "Organic Dragonfruit", "in_stock": False},
        ]
        ingredient = {
            "ingredient_key": "dragonfruit",
            "recipe_requirement": "1 fresh dragonfruit",
            "heb_search_query": "Dragonfruit",
        }

        match = self.client.match_ingredient_to_sku(ingredient, candidate_pool=candidates)
        self.assertEqual(match["tier"], 4)
        self.assertEqual(match["status"], "NEEDS_CALL")
        self.assertTrue(match["needs_human_call"])
        self.assertIsNone(match["sku"])


if __name__ == "__main__":
    unittest.main()
