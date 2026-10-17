#!/usr/bin/env python3
"""
scripts/cache_manager.py

Standalone YAML Recipe Cache Manager, Pantry Comparator, and Household Scaler.
Handles recipe serialization to cache/recipes/<slug>.yaml, index lookups,
and SKU promotions.
"""

import os
import sys
import json
import re
import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple, Union
import yaml
from pydantic import BaseModel, Field


# ==============================================================================
# 1. Pydantic Models
# ==============================================================================

class HEBCartItem(BaseModel):
    """Represents a specific matched H-E-B product SKU."""
    heb_product_id: str
    heb_product_name: str
    package_size: Optional[str] = None
    quantity_to_order: int = 1
    price: Optional[float] = None
    reason: Optional[str] = None


class ScaledIngredient(BaseModel):
    """Represents an ingredient requirement scaled to household portions."""
    ingredient_key: str
    recipe_requirement: str
    original_requirement: Optional[str] = None
    heb_search_query: str
    is_pantry_staple: bool = False
    is_kid_sensitive: bool = False
    primary_sku: Optional[HEBCartItem] = None
    fallback_skus: List[HEBCartItem] = Field(default_factory=list)


class RecipeItem(BaseModel):
    """Basic representation of an extracted recipe item."""
    id: str
    title: str
    source_url: str
    video_id: str
    platform: str = "tiktok"
    servings_original: float = 4.0
    instructions: List[str] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)


class RecipeCache(BaseModel):
    """Full persistent schema stored in cache/recipes/<slug>.yaml."""
    id: str
    title: str
    source_url: str
    video_id: str
    platform: str = "tiktok"
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    last_ordered: Optional[str] = None
    times_ordered: int = 0
    tags: List[str] = Field(default_factory=list)
    scaling: Dict[str, Any] = Field(default_factory=dict)
    kid_adaptations: List[str] = Field(default_factory=list)
    ingredients_to_buy: List[ScaledIngredient] = Field(default_factory=list)
    pantry_staples_skipped: List[Dict[str, Any]] = Field(default_factory=list)
    instructions: List[str] = Field(default_factory=list)


# ==============================================================================
# 2. Pantry Staples Comparator
# ==============================================================================

class PantryComparator:
    """Checks whether ingredients match household pantry staples to skip or mark optional."""

    def __init__(self, config_path: Optional[str] = None):
        self.staples: List[Dict[str, Any]] = []
        self._load_staples(config_path)

    def _load_staples(self, custom_path: Optional[str] = None):
        candidates = [
            custom_path,
            "config/pantry_staples.json",
            "config/pantry_staples.example.json",
            Path(__file__).resolve().parent.parent / "config/pantry_staples.json",
            Path(__file__).resolve().parent.parent / "config/pantry_staples.example.json",
        ]
        loaded = False
        for p in candidates:
            if p and os.path.exists(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        self.staples = data.get("staples", [])
                        loaded = True
                        break
                except Exception:
                    pass
        if not loaded:
            self.staples = []

    def check_staple(self, ingredient_name: str) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """
        Returns (is_staple, matched_staple_dict).
        Compares by name and aliases.
        """
        text = ingredient_name.lower().strip()
        words = set(re.findall(r"\b[a-zA-Z]+\b", text))

        for item in self.staples:
            canonical = item.get("name", "").lower()
            aliases = [a.lower() for a in item.get("aliases", [])]

            # Direct substring or exact phrase match
            if canonical in text:
                return True, item
            for alias in aliases:
                if alias in text:
                    return True, item

            # Word boundary matching for common single-word staples
            if canonical in words:
                return True, item

        return False, None


# ==============================================================================
# 3. Household Scaler & Kid Adaptation Splitter
# ==============================================================================

KID_SPICE_PATTERNS = [
    r"\bsriracha\b", r"\bchili\b", r"\bchilli\b", r"\bjalape[nñ]o\b",
    r"\bcayenne\b", r"\bgochujang\b", r"\bred pepper flakes\b",
    r"\bcrushed red pepper\b", r"\bhot sauce\b", r"\bhabanero\b",
    r"\bchili crisp\b", r"\bchipotle\b"
]


class HouseholdScaler:
    """Scales recipes and generates kid-friendly culinary adaptations."""

    def __init__(self, config_path: Optional[str] = None):
        self.serving_target = 2.5
        self.kid_friendly_split = True
        self.min_scale = 0.65
        self._load_config(config_path)

    def _load_config(self, custom_path: Optional[str] = None):
        candidates = [
            custom_path,
            "config/household.yaml",
            "config/household.example.yaml",
            Path(__file__).resolve().parent.parent / "config/household.yaml",
            Path(__file__).resolve().parent.parent / "config/household.example.yaml",
        ]
        for p in candidates:
            if p and os.path.exists(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        cfg = yaml.safe_load(f) or {}
                        hh = cfg.get("household", {})
                        self.serving_target = float(hh.get("serving_target", 2.5))
                        self.kid_friendly_split = bool(hh.get("kid_friendly_split", True))
                        profile = cfg.get("scaling_profile", {})
                        self.min_scale = float(profile.get("leftover_buffer_min_scale", 0.65))
                        break
                except Exception:
                    pass

    def calculate_scale_factor(self, original_servings: float = 4.0) -> float:
        if original_servings <= 0:
            original_servings = 4.0
        factor = self.serving_target / original_servings
        # Avoid pan dry-out undercrowding
        return max(self.min_scale, round(factor, 3))

    def is_kid_sensitive(self, text: str) -> bool:
        lower = text.lower()
        return any(re.search(pat, lower) for pat in KID_SPICE_PATTERNS)

    def generate_kid_adaptations(self, ingredients: List[str], instructions: List[str]) -> List[str]:
        adaptations = []
        spicy_items = [ing for ing in ingredients if self.is_kid_sensitive(ing)]

        if spicy_items:
            joined = ", ".join(spicy_items[:3])
            adaptations.append(
                f"Decouple heat ({joined}): Reserve heat elements out of the main vessel. "
                "Plate the 4-year-old's portion first, then toss adult portions with spicy sauce at the end."
            )

        # Protein size reminder
        has_protein = any(
            any(k in ing.lower() for k in ["salmon", "chicken", "beef", "steak", "pork", "tofu"])
            for ing in ingredients
        )
        if has_protein:
            adaptations.append("Cut protein into 1/2-inch bite-sized cubes for the child's portion.")

        return adaptations


# ==============================================================================
# 4. Recipe Cache Manager
# ==============================================================================

class CacheManager:
    """Manages cache/recipes/<slug>.yaml and cache/index.json."""

    def __init__(self, cache_dir: str = "cache"):
        self.cache_dir = Path(cache_dir)
        self.recipes_dir = self.cache_dir / "recipes"
        self.index_file = self.cache_dir / "index.json"
        self.recipes_dir.mkdir(parents=True, exist_ok=True)
        self.pantry = PantryComparator()
        self.scaler = HouseholdScaler()

    def _load_index(self) -> Dict[str, Any]:
        if self.index_file.exists():
            try:
                with open(self.index_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def _save_index(self, index_data: Dict[str, Any]):
        with open(self.index_file, "w", encoding="utf-8") as f:
            json.dump(index_data, f, indent=2)

    def lookup(self, query: str) -> Tuple[str, Optional[Dict[str, Any]]]:
        """
        Lookup recipe in cache by video_id, URL, title slug, or keywords.
        Returns ("HIT"|"MISS", recipe_dict).
        """
        index = self._load_index()
        query_clean = query.strip().lower()

        # Check direct index match
        matched_slug = None
        for key, slug in index.items():
            if key.lower() == query_clean or query_clean in key.lower():
                matched_slug = slug
                break

        # Check existing yaml files directly if not in index
        if not matched_slug:
            for yaml_path in self.recipes_dir.glob("*.yaml"):
                slug = yaml_path.stem
                if slug in query_clean or query_clean in slug:
                    matched_slug = slug
                    break

        if matched_slug:
            recipe_path = self.recipes_dir / f"{matched_slug}.yaml"
            if recipe_path.exists():
                try:
                    with open(recipe_path, "r", encoding="utf-8") as f:
                        data = yaml.safe_load(f)
                        return "HIT", data
                except Exception:
                    pass

        return "MISS", None

    def save(self, recipe_cache: Union[RecipeCache, Dict[str, Any]]) -> str:
        """
        Validates schema, writes to cache/recipes/<slug>.yaml, updates index.json.
        Returns the saved file path.
        """
        if isinstance(recipe_cache, dict):
            recipe_obj = RecipeCache.model_validate(recipe_cache)
        else:
            recipe_obj = recipe_cache

        slug = re.sub(r"[^a-z0-9]+", "-", recipe_obj.id.lower()).strip("-")
        if not slug:
            slug = re.sub(r"[^a-z0-9]+", "-", recipe_obj.title.lower()).strip("-")

        file_path = self.recipes_dir / f"{slug}.yaml"

        # Dump to YAML
        data = recipe_obj.model_dump()
        with open(file_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, sort_keys=False, default_flow_style=False)

        # Update index
        index = self._load_index()
        index[slug] = slug
        if recipe_obj.video_id:
            index[recipe_obj.video_id] = slug
        if recipe_obj.source_url:
            index[recipe_obj.source_url] = slug
        self._save_index(index)

        return str(file_path)

    def promote_sku(
        self,
        recipe_id: str,
        ingredient_key: str,
        product_id: str,
        name: str,
        qty: int = 1,
    ) -> bool:
        """
        Updates primary_sku only upon explicit human confirmation.
        """
        hit_status, recipe_data = self.lookup(recipe_id)
        if hit_status != "HIT" or not recipe_data:
            return False

        updated = False
        ingredients = recipe_data.get("ingredients_to_buy", [])
        for item in ingredients:
            if item.get("ingredient_key") == ingredient_key:
                old_primary = item.get("primary_sku")
                new_sku = {
                    "heb_product_id": str(product_id),
                    "heb_product_name": name,
                    "quantity_to_order": int(qty),
                    "reason": "Promoted by user command",
                }
                # Move old primary to fallbacks
                if old_primary and old_primary.get("heb_product_id") != str(product_id):
                    item.setdefault("fallback_skus", []).insert(0, old_primary)

                item["primary_sku"] = new_sku
                updated = True
                break

        if updated:
            self.save(recipe_data)
            return True
        return False


# ==============================================================================
# 5. CLI Interface
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(description="Standalone YAML Recipe Cache Manager")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # lookup command
    lookup_parser = subparsers.add_parser("lookup", help="Lookup recipe in cache")
    lookup_parser.add_argument("--query", required=True, help="URL, video ID, or title slug")

    # save command
    save_parser = subparsers.add_parser("save", help="Save recipe YAML into cache")
    save_parser.add_argument("--file", required=True, help="Path to YAML or JSON recipe file")

    # promote-sku command
    promote_parser = subparsers.add_parser("promote-sku", help="Promote a SKU to primary_sku")
    promote_parser.add_argument("--recipe-id", required=True, help="Recipe ID or slug")
    promote_parser.add_argument("--ingredient-key", required=True, help="Ingredient key")
    promote_parser.add_argument("--product-id", required=True, help="New H-E-B product ID")
    promote_parser.add_argument("--name", required=True, help="Product name")
    promote_parser.add_argument("--qty", type=int, default=1, help="Quantity to order")

    args = parser.parse_args()
    mgr = CacheManager()

    if args.command == "lookup":
        status, data = mgr.lookup(args.query)
        output = {"status": status}
        if data:
            output["recipe"] = data
        print(json.dumps(output, indent=2))

    elif args.command == "save":
        with open(args.file, "r", encoding="utf-8") as f:
            if args.file.endswith(".json"):
                data = json.load(f)
            else:
                data = yaml.safe_load(f)
        saved_path = mgr.save(data)
        print(json.dumps({"status": "SAVED", "path": saved_path}))

    elif args.command == "promote-sku":
        success = mgr.promote_sku(
            recipe_id=args.recipe_id,
            ingredient_key=args.ingredient_key,
            product_id=args.product_id,
            name=args.name,
            qty=args.qty,
        )
        print(json.dumps({"status": "SUCCESS" if success else "NOT_FOUND"}))


if __name__ == "__main__":
    main()
