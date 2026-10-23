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

_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))


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


class PreferredSKU(BaseModel):
    """Represents a preferred product SKU mapping for a canonical ingredient."""
    canonical_key: str
    aliases: List[str] = Field(default_factory=list)
    product_id: str
    sku_id: str
    display_name: str
    brand: str = "H-E-B"
    package_size: Optional[str] = None
    unit_size: Optional[float] = None
    unit: Optional[str] = None
    source: str = "past_orders"  # "past_orders" or "manual_override"
    last_purchased: Optional[str] = None
    effective_weight: float = 1.0
    order_count: int = 1


class PreferredSKUsCache(BaseModel):
    """Full persistent schema stored in cache/preferred_skus.json."""
    schema_version: str = "1.0"
    last_synced: Optional[str] = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    preferred_skus: Dict[str, PreferredSKU] = Field(default_factory=dict)


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
# 4. Culinary Normalization, Non-Food Filtering & Weight Decay Engine
# ==============================================================================

NON_FOOD_CATEGORIES = {
    "household", "cleaning", "paper goods", "pet", "baby", "health & beauty",
    "beauty", "pharmacy", "personal care", "laundry", "office", "patio & garden",
    "home & outdoor", "electronics", "automotive", "hardware"
}

NON_FOOD_KEYWORDS = [
    r"\bpaper towels?\b", r"\btoilet paper\b", r"\bbath tissue\b",
    r"\btrash bags?\b", r"\bgarbage bags?\b", r"\blaundry detergent\b",
    r"\bdish(?:washing)? (?:liquid|soap|pods|detergent)\b", r"\bhand soap\b",
    r"\bbody wash\b", r"\bshampoo\b", r"\bconditioner\b", r"\btoothpaste\b",
    r"\btoothbrushes?\b", r"\bdeodorant\b", r"\bcat food\b", r"\bdog food\b",
    r"\bpet food\b", r"\bcat litter\b", r"\bdisinfecting wipes\b",
    r"\bbleach\b", r"\bsponges?\b", r"\bfabric softener\b", r"\bfacial tissue\b",
    r"\bnapkins?\b", r"\bdiapers?\b", r"\bbaby wipes\b", r"\bbatteries?\b",
    r"\blawn\b", r"\bfoil\b", r"\bparchment paper\b", r"\bplastic wrap\b",
    r"\bziploc\b", r"\bstorage bags?\b", r"\bfreezer bags?\b",
]

CULINARY_RULES = [
    # Compound bakery & dairy first
    (r"\b(?:mini\s+)?(?:butter\s+)?croissants?\b", "croissants", ["croissants", "mini butter croissants", "butter croissants"]),
    (r"\bgreek\s+yogurt\s+sandwiches?\b", "greek yogurt sandwiches", ["greek yogurt sandwiches", "ice cream sandwiches"]),
    (r"\bgreek\s+yogurt\b", "greek yogurt", ["greek yogurt", "yogurt"]),
    (r"\bcream\s+cheese\b", "cream cheese", ["cream cheese"]),
    (r"\b(?:sharp|mild|medium|white)?\s*cheddar(?:\s+cheese)?\b", "cheddar cheese", ["cheddar cheese", "sharp cheddar cheese", "cheddar"]),
    (r"\bmozzarella(?:\s+cheese)?\b", "mozzarella cheese", ["mozzarella cheese", "mozzarella"]),
    (r"\b(?:parmesan|parmigiano(?:[- ]reggiano)?)(?:\s+cheese)?\b", "parmesan cheese", ["parmesan cheese", "parmesan", "parmigiano"]),
    (r"\bheavy(?:\s+whipping)?\s+cream\b", "heavy cream", ["heavy cream", "heavy whipping cream", "whipping cream"]),
    (r"\bhalf\s+(?:and|&)\s+half\b", "half and half", ["half and half", "half & half"]),
    (r"\bsour\s+cream\b", "sour cream", ["sour cream"]),
    (r"\bsourdough\s+flatbread\b", "sourdough flatbread", ["sourdough flatbread", "flatbread"]),
    (r"\bsourdough\s+bread\b", "sourdough bread", ["sourdough bread", "bread", "sourdough"]),
    (r"\b(?:flour\s+|corn\s+|butter\s+)?tortillas?\b", "tortillas", ["tortillas", "flour tortillas", "corn tortillas"]),
    (r"\bbread\b", "bread", ["bread"]),
    (r"\b(?:homestyle\s+|meat\s+)?lasagna\b", "lasagna", ["lasagna", "meat lasagna"]),
    # Base dairy & eggs
    (r"\b(?:grade a\s+)?(?:large\s+)?(?:cage[- ]free\s+|pasture[- ]raised\s+)?(?:brown\s+|white\s+)?eggs?\b", "eggs", ["eggs", "large eggs", "brown eggs", "large brown eggs", "egg"]),
    (r"\bwhole\s+milk\b", "whole milk", ["whole milk", "milk"]),
    (r"\b(?:reduced fat\s+)?2%\s+milk\b", "2% milk", ["2% milk", "reduced fat milk", "milk"]),
    (r"\b(?:low fat\s+)?1%\s+milk\b", "1% milk", ["1% milk", "low fat milk", "milk"]),
    (r"\b(?:skim|fat[- ]free|nonfat)\s+milk\b", "skim milk", ["skim milk", "fat free milk", "milk"]),
    (r"\balmond\s+milk\b", "almond milk", ["almond milk"]),
    (r"\boat\s+milk\b", "oat milk", ["oat milk"]),
    (r"\bcoconut\s+milk\b", "coconut milk", ["coconut milk"]),
    (r"\bmilk\b", "milk", ["milk"]),
    (r"\b(?:salted|unsalted|pure irish|grass[- ]fed|sweet cream)?\s*butter(?!\s+(?:tortillas?|croissants?|bread|cookies?))\b", "butter", ["butter", "salted butter", "unsalted butter"]),
    (r"\bcheese\b", "cheese", ["cheese"]),
    (r"\byogurt\b", "yogurt", ["yogurt"]),
    # Produce
    (r"\b(?:organic\s+)?bananas?\b", "bananas", ["bananas", "organic bananas", "banana"]),
    (r"\b(?:hass\s+)?avocados?\b", "avocados", ["avocados", "avocado", "hass avocados"]),
    (r"\byellow\s+onions?\b", "yellow onion", ["yellow onion", "yellow onions", "onion", "onions"]),
    (r"\bred\s+onions?\b", "red onion", ["red onion", "red onions", "onion", "onions"]),
    (r"\bwhite\s+onions?\b", "white onion", ["white onion", "white onions", "onion", "onions"]),
    (r"\b(?:green\s+onions?|scallions?|spring\s+onions?)\b", "green onion", ["green onion", "green onions", "scallions", "spring onions"]),
    (r"\bonions?\b", "onion", ["onion", "onions"]),
    (r"\bgarlic(?:\s+cloves?|\s+bulb)?\b", "garlic", ["garlic", "garlic cloves"]),
    (r"\broma\s+tomatoes?\b", "roma tomatoes", ["roma tomatoes", "tomatoes", "roma tomato"]),
    (r"\btomatoes?\b", "tomatoes", ["tomatoes", "tomato"]),
    (r"\blemons?\b", "lemons", ["lemons", "lemon"]),
    (r"\blimes?\b", "limes", ["limes", "lime"]),
    (r"\bstrawberries?\b", "strawberries", ["strawberries", "strawberry"]),
    (r"\bblueberries?\b", "blueberries", ["blueberries", "blueberry"]),
    (r"\bapples?\b", "apples", ["apples", "apple"]),
    (r"\brusset\s+potatoes?\b", "russet potatoes", ["russet potatoes", "potatoes", "russet potato"]),
    (r"\bpotatoes?\b", "potatoes", ["potatoes", "potato"]),
    (r"\b(?:sweet\s+potatoes?|yams?)\b", "sweet potatoes", ["sweet potatoes", "sweet potato", "yams"]),
    (r"\bbell\s+peppers?\b", "bell pepper", ["bell pepper", "bell peppers"]),
    (r"\bcarrots?\b", "carrots", ["carrots", "carrot"]),
    (r"\bcelery(?:\s+stalks?)?\b", "celery", ["celery"]),
    (r"\b(?:baby\s+)?spinach\b", "spinach", ["spinach", "baby spinach"]),
    (r"\bbroccoli(?:\s+florets?|\s+crowns?)?\b", "broccoli", ["broccoli"]),
    # Meats
    (r"\bground\s+beef(?:\s+chuck|\s+round|\s+sirloin)?\b", "ground beef", ["ground beef", "ground beef chuck", "ground chuck"]),
    (r"\bground\s+turkey\b", "ground turkey", ["ground turkey"]),
    (r"\b(?:boneless\s+skinless\s+)?chicken\s+breasts?\b", "chicken breast", ["chicken breast", "chicken breasts", "boneless skinless chicken breast"]),
    (r"\b(?:boneless\s+skinless\s+)?chicken\s+thighs?\b", "chicken thighs", ["chicken thighs", "boneless skinless chicken thighs"]),
    (r"\b(?:thick\s+cut\s+|applewood\s+smoked\s+)?bacon\b", "bacon", ["bacon", "thick cut bacon"]),
    (r"\bsalmon(?:\s+fillets?)?\b", "salmon", ["salmon", "salmon fillets", "atlantic salmon"]),
    (r"\bshrimp\b", "shrimp", ["shrimp"]),
    # Grains & Pantry
    (r"\bjasmine\s+rice\b", "jasmine rice", ["jasmine rice", "rice", "white rice"]),
    (r"\bbasmati\s+rice\b", "basmati rice", ["basmati rice", "rice"]),
    (r"\brice\b", "rice", ["rice"]),
    (r"\b(?:pasta|spaghetti|penne|rigatoni|fusilli|fettuccine)\b", "pasta", ["pasta", "spaghetti", "penne"]),
    (r"\b(?:extra\s+virgin\s+)?olive\s+oil\b", "olive oil", ["olive oil", "extra virgin olive oil", "evoo"]),
    (r"\b(?:vegetable|canola)\s+oil\b", "vegetable oil", ["vegetable oil", "canola oil"]),
    (r"\bchicken\s+(?:broth|stock|bone broth)\b", "chicken broth", ["chicken broth", "chicken stock"]),
    (r"\bbeef\s+(?:broth|stock|bone broth)\b", "beef broth", ["beef broth", "beef stock"]),
    (r"\b(?:sparkling\s+water|seltzer)\b", "sparkling water", ["sparkling water", "seltzer"]),
    (r"\b(?:pure\s+|raw\s+|clover\s+)?honey\b", "honey", ["honey", "raw honey"]),
]

BRAND_PREFIXES = [
    r"^(?:h-e-b|heb|central market(?: organics)?|hill country fare|mi tienda|chobani|yasso|kerrygold|rustic bakery|kraft|sargento|tyson|perdue|foster farms|driscoll\'?s|fresh bunch of|fresh)\s+",
]

MARKETING_WORDS = [
    r"\b(?:organics?|grade a|100% pure|pure|all natural|natural|homestyle|traditional|premium|frozen|fresh|low[- ]fat|non[- ]fat|reduced fat)\b",
]

SIZE_SUFFIXES = [
    r"(?:,\s*|\s*-\s*|\s+)(?:avg\.?\s*\d+(?:\.\d+)?\s*(?:lbs?|oz)|large family-size|family size|party size|\d+(?:\.\d+)?\s*(?:ct|oz|lb|lbs|gal|g|ml|fl oz|count|pk|pack|bananas?)).*$",
]


def is_food_item(item: Dict[str, Any]) -> bool:
    """Returns True if item is edible food/beverage; False if household/non-food."""
    cat = (item.get("category") or "").lower()
    dept = (item.get("department") or "").lower()
    for nfc in NON_FOOD_CATEGORIES:
        if nfc in cat or nfc in dept:
            return False

    name = (
        item.get("display_name")
        or item.get("fullDisplayName")
        or item.get("name")
        or ""
    ).lower()

    for pat in NON_FOOD_KEYWORDS:
        if re.search(pat, name):
            return False

    return True


def extract_culinary_info(name: str) -> Tuple[str, List[str]]:
    """
    Extracts canonical culinary key and list of aliases from a product name.
    First checks curated culinary knowledge rules. If unmatched, cleans
    brands, marketing terms, and package sizes using NLP heuristics.
    """
    lower = name.lower().strip()
    for pattern, canonical, aliases in CULINARY_RULES:
        if re.search(pattern, lower):
            return canonical, list(aliases)

    cleaned = lower
    for bp in BRAND_PREFIXES:
        cleaned = re.sub(bp, "", cleaned, flags=re.IGNORECASE)
    for ss in SIZE_SUFFIXES:
        cleaned = re.sub(ss, "", cleaned, flags=re.IGNORECASE)
    for mw in MARKETING_WORDS:
        cleaned = re.sub(mw, "", cleaned, flags=re.IGNORECASE)

    cleaned = re.sub(r"[^a-z0-9\s-]", "", cleaned)
    cleaned = " ".join(cleaned.split()).strip()

    if not cleaned:
        cleaned = name.lower().strip()

    aliases = [cleaned]
    if cleaned.endswith("s") and len(cleaned) > 3:
        aliases.append(cleaned[:-1])
    else:
        aliases.append(cleaned + "s")

    return cleaned, sorted(list(set(aliases)))


def parse_package_size(pkg_size_str: Optional[str]) -> Tuple[Optional[float], Optional[str]]:
    """
    Parses package size strings like '12 ct', '1/2 gal', '16 oz', '1 lb', 'Avg. 2.425 lbs'.
    Returns (unit_size, unit).
    """
    if not pkg_size_str or not isinstance(pkg_size_str, str):
        return None, None

    text = pkg_size_str.strip().lower()
    text = re.sub(r"^(?:avg\.?|approx\.?)\s*", "", text)

    frac_match = re.match(r"^(\d+)/(\d+)\s*([a-zA-Z]+.*)$", text)
    if frac_match:
        num, den, unit = frac_match.groups()
        try:
            val = round(float(num) / float(den), 3)
            unit_clean = unit.strip()
            if unit_clean.endswith("s") and unit_clean not in ["lbs", "fl oz"]:
                unit_clean = unit_clean[:-1]
            if unit_clean == "lbs":
                unit_clean = "lb"
            return val, unit_clean
        except Exception:
            pass

    match = re.match(r"^([\d\.]+)\s*([a-zA-Z]+.*)$", text)
    if match:
        val_str, unit = match.groups()
        try:
            val = float(val_str)
            if val.is_integer():
                val = int(val)
            unit_clean = unit.strip()
            if unit_clean.endswith("s") and unit_clean not in ["lbs", "fl oz"]:
                unit_clean = unit_clean[:-1]
            if unit_clean == "lbs":
                unit_clean = "lb"
            elif unit_clean == "bananas":
                unit_clean = "ct"
            return val, unit_clean
        except Exception:
            pass

    return None, None


def calculate_effective_weight(
    order_count: int,
    last_purchased: Optional[str] = None,
    ref_time: Optional[datetime] = None,
) -> float:
    """
    Calculates time-decay weighted score on order_count.
    Purchases in the last 30 days are weighted 3x higher.
    Beyond 30 days, exponential decay is applied (half-life 45 days, min multiplier 0.2).
    """
    if order_count <= 0:
        order_count = 1

    days_ago = 60.0
    if last_purchased:
        try:
            clean = last_purchased.strip()
            if clean.endswith("Z"):
                clean = clean[:-1] + "+00:00"
            dt = datetime.fromisoformat(clean)
            now = ref_time or datetime.now(timezone.utc)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            days_ago = max(0.0, (now - dt).total_seconds() / 86400.0)
        except Exception:
            days_ago = 60.0

    if days_ago <= 30.0:
        multiplier = 3.0
    else:
        multiplier = max(0.2, 3.0 * (0.5 ** ((days_ago - 30.0) / 45.0)))

    return round(float(order_count * multiplier), 2)


# ==============================================================================
# 5. Recipe Cache Manager
# ==============================================================================

class CacheManager:
    """Manages cache/recipes/<slug>.yaml, cache/index.json, and cache/preferred_skus.json."""

    def __init__(self, cache_dir: str = "cache"):
        self.cache_dir = Path(cache_dir)
        self.recipes_dir = self.cache_dir / "recipes"
        self.index_file = self.cache_dir / "index.json"
        self.preferred_skus_file = self.cache_dir / "preferred_skus.json"
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

    def _load_preferred_skus(self) -> Dict[str, Any]:
        """Loads cache/preferred_skus.json or initializes empty schema."""
        if self.preferred_skus_file.exists():
            try:
                with open(self.preferred_skus_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {
            "schema_version": "1.0",
            "last_synced": None,
            "preferred_skus": {},
        }

    def _save_preferred_skus(self, data: Dict[str, Any]):
        """Persists data to cache/preferred_skus.json."""
        self.preferred_skus_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.preferred_skus_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def pin_preferred_sku(
        self,
        canonical_key: str,
        product_id: str,
        sku_id: Optional[str] = None,
        name: str = "",
        brand: str = "H-E-B",
        package_size: Optional[str] = None,
        unit_size: Optional[float] = None,
        unit: Optional[str] = None,
        aliases: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Pins a preferred SKU manual override in cache/preferred_skus.json.
        Takes highest priority over past order automatic syncing unless --force is used.
        """
        key = canonical_key.lower().strip()
        data = self._load_preferred_skus()
        current_time = datetime.now(timezone.utc).isoformat()

        if package_size and (unit_size is None or unit is None):
            u_sz, u_nm = parse_package_size(package_size)
            unit_size = unit_size or u_sz
            unit = unit or u_nm

        if not aliases:
            _, detected_aliases = extract_culinary_info(name or key)
            aliases = list(set([key] + detected_aliases))

        entry = {
            "canonical_key": key,
            "aliases": sorted(list(set(aliases))),
            "product_id": str(product_id),
            "sku_id": str(sku_id or product_id),
            "display_name": name or key,
            "brand": brand or "H-E-B",
            "package_size": package_size or "",
            "unit_size": unit_size,
            "unit": unit,
            "source": "manual_override",
            "last_purchased": current_time,
            "effective_weight": 999.0,
            "order_count": 1,
        }

        data["schema_version"] = "1.0"
        data["last_synced"] = current_time
        data.setdefault("preferred_skus", {})[key] = entry
        self._save_preferred_skus(data)
        return entry

    def update_preferred_skus(
        self,
        force: bool = False,
        store_id: str = "639",
        limit: int = 50,
        client: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """
        Syncs preferred SKUs from H-E-B past orders and buy-it-again endpoints.
        Ranks competing products by recency + count using exponential time decay.
        Preserves manual overrides unless force=True.
        Fails loudly if unauthenticated or blocked by WAF.
        """
        if client is None:
            from scripts.heb_graphql import HEBGraphQLClient
            client = HEBGraphQLClient(store_id=store_id)

        # 1. Fetch raw items from Buy It Again and Past Orders
        buy_it_again = client.fetch_buy_it_again_items(store_id=store_id, limit=limit)
        past_orders = client.fetch_past_order_items(limit=limit)

        all_raw_items = (buy_it_again or []) + (past_orders or [])

        # 2. Filter non-food and aggregate by product_id
        product_aggregates: Dict[str, Dict[str, Any]] = {}
        for item in all_raw_items:
            if not is_food_item(item):
                continue

            pid = str(item.get("product_id") or "").strip()
            if not pid:
                continue

            count = int(item.get("purchase_count") or 1)
            date_str = item.get("last_purchased") or ""

            if pid not in product_aggregates:
                product_aggregates[pid] = {
                    "product_id": pid,
                    "sku_id": str(item.get("sku_id") or pid),
                    "display_name": item.get("display_name") or "",
                    "brand": item.get("brand") or "H-E-B",
                    "package_size": item.get("package_size") or "",
                    "order_count": count,
                    "last_purchased": date_str,
                    "category": item.get("category") or "",
                    "department": item.get("department") or "",
                }
            else:
                agg = product_aggregates[pid]
                agg["order_count"] += count
                if date_str:
                    if not agg["last_purchased"] or date_str > agg["last_purchased"]:
                        agg["last_purchased"] = date_str

        # 3. Group by canonical culinary key
        candidates_by_key: Dict[str, List[Dict[str, Any]]] = {}
        for pid, prod in product_aggregates.items():
            ckey, aliases = extract_culinary_info(prod["display_name"])
            if not ckey:
                continue
            u_sz, u_nm = parse_package_size(prod.get("package_size"))
            weight = calculate_effective_weight(prod["order_count"], prod["last_purchased"])
            prod_candidate = dict(prod)
            prod_candidate["canonical_key"] = ckey
            prod_candidate["aliases"] = aliases
            prod_candidate["unit_size"] = u_sz
            prod_candidate["unit"] = u_nm
            prod_candidate["effective_weight"] = weight
            candidates_by_key.setdefault(ckey, []).append(prod_candidate)

        # 4. For each canonical key, select winner by effective_weight
        existing_data = self._load_preferred_skus()
        current_preferred = existing_data.get("preferred_skus", {})
        now_iso = datetime.now(timezone.utc).isoformat()

        for ckey, candidates in candidates_by_key.items():
            # Check manual override protection
            if not force and ckey in current_preferred:
                if current_preferred[ckey].get("source") == "manual_override":
                    continue

            # Sort by effective weight descending, tie-break by last_purchased descending
            candidates.sort(key=lambda c: (c["effective_weight"], c["last_purchased"] or ""), reverse=True)
            winner = candidates[0]

            # Merge aliases from all candidates for this key
            all_aliases = set(winner["aliases"])
            all_aliases.add(ckey)
            for c in candidates:
                all_aliases.update(c.get("aliases", []))

            current_preferred[ckey] = {
                "canonical_key": ckey,
                "aliases": sorted(list(all_aliases)),
                "product_id": winner["product_id"],
                "sku_id": winner["sku_id"],
                "display_name": winner["display_name"],
                "brand": winner["brand"],
                "package_size": winner.get("package_size") or "",
                "unit_size": winner.get("unit_size"),
                "unit": winner.get("unit"),
                "source": "past_orders",
                "last_purchased": winner.get("last_purchased") or "",
                "effective_weight": winner["effective_weight"],
                "order_count": winner["order_count"],
            }

        existing_data["schema_version"] = "1.0"
        existing_data["last_synced"] = now_iso
        existing_data["preferred_skus"] = current_preferred
        self._save_preferred_skus(existing_data)
        return existing_data

    def promote_sku(
        self,
        recipe_id: str,
        ingredient_key: str,
        product_id: str,
        name: str,
        qty: int = 1,
        brand: str = "H-E-B",
        sku_id: Optional[str] = None,
        package_size: Optional[str] = None,
    ) -> bool:
        """
        Updates primary_sku in the recipe YAML and pins the item into
        cache/preferred_skus.json with source="manual_override".
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
                    "package_size": package_size or (old_primary.get("package_size") if old_primary else None),
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
            # Pin to preferred_skus.json with manual_override
            self.pin_preferred_sku(
                canonical_key=ingredient_key,
                product_id=str(product_id),
                sku_id=str(sku_id or product_id),
                name=name,
                brand=brand or "H-E-B",
                package_size=package_size,
            )
            return True
        return False


# ==============================================================================
# Module Helpers
# ==============================================================================

def update_preferred_skus(
    cache_dir: str = "cache",
    force: bool = False,
    store_id: str = "639",
    limit: int = 50,
    client: Optional[Any] = None,
) -> Dict[str, Any]:
    """Syncs preferred SKUs from H-E-B past orders and buy-it-again."""
    mgr = CacheManager(cache_dir=cache_dir)
    return mgr.update_preferred_skus(force=force, store_id=store_id, limit=limit, client=client)


def pin_preferred_sku(
    canonical_key: str,
    product_id: str,
    sku_id: Optional[str] = None,
    name: str = "",
    brand: str = "H-E-B",
    package_size: Optional[str] = None,
    unit_size: Optional[float] = None,
    unit: Optional[str] = None,
    aliases: Optional[List[str]] = None,
    cache_dir: str = "cache",
) -> Dict[str, Any]:
    """Pins a preferred SKU manual override."""
    mgr = CacheManager(cache_dir=cache_dir)
    return mgr.pin_preferred_sku(
        canonical_key=canonical_key,
        product_id=product_id,
        sku_id=sku_id,
        name=name,
        brand=brand,
        package_size=package_size,
        unit_size=unit_size,
        unit=unit,
        aliases=aliases,
    )


# ==============================================================================
# 6. CLI Interface
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(description="Standalone YAML Recipe Cache Manager & Preferred SKUs Engine")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # lookup command
    lookup_parser = subparsers.add_parser("lookup", help="Lookup recipe in cache")
    lookup_parser.add_argument("--query", required=True, help="URL, video ID, or title slug")

    # save command
    save_parser = subparsers.add_parser("save", help="Save recipe YAML into cache")
    save_parser.add_argument("--file", required=True, help="Path to YAML or JSON recipe file")

    # promote-sku command
    promote_parser = subparsers.add_parser("promote-sku", help="Promote a SKU to primary_sku and pin preferred SKU")
    promote_parser.add_argument("--recipe-id", required=True, help="Recipe ID or slug")
    promote_parser.add_argument("--ingredient-key", required=True, help="Ingredient key")
    promote_parser.add_argument("--product-id", required=True, help="New H-E-B product ID")
    promote_parser.add_argument("--name", required=True, help="Product name")
    promote_parser.add_argument("--qty", type=int, default=1, help="Quantity to order")
    promote_parser.add_argument("--brand", default="H-E-B", help="Product brand (default 'H-E-B')")
    promote_parser.add_argument("--sku", default=None, help="H-E-B SKU ID (optional)")
    promote_parser.add_argument("--package-size", default=None, help="Package size (optional)")

    # update-preferred-skus command
    update_skus_parser = subparsers.add_parser(
        "update-preferred-skus",
        help="Sync preferred SKUs from H-E-B past orders and buy-it-again",
    )
    update_skus_parser.add_argument("--force", action="store_true", help="Force overwrite manual overrides")
    update_skus_parser.add_argument("--store-id", default="639", help="H-E-B store ID (default 639)")
    update_skus_parser.add_argument("--limit", type=int, default=50, help="Max items to fetch from order history")

    # pin-preferred-sku command
    pin_parser = subparsers.add_parser("pin-preferred-sku", help="Pin a preferred SKU manual override")
    pin_parser.add_argument("--ingredient", required=True, help="Canonical ingredient key (e.g. 'whole milk')")
    pin_parser.add_argument("--sku", required=True, help="H-E-B SKU ID")
    pin_parser.add_argument("--product", required=True, help="H-E-B Product ID")
    pin_parser.add_argument("--name", required=True, help="Product display name")
    pin_parser.add_argument("--brand", default="H-E-B", help="Product brand (default 'H-E-B')")
    pin_parser.add_argument("--package-size", default=None, help="Package size (e.g. '1 gal', '12 ct')")

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
            brand=args.brand,
            sku_id=args.sku,
            package_size=args.package_size,
        )
        print(json.dumps({"status": "SUCCESS" if success else "NOT_FOUND"}))

    elif args.command == "update-preferred-skus":
        data = mgr.update_preferred_skus(
            force=args.force,
            store_id=args.store_id,
            limit=args.limit,
        )
        print(json.dumps(data, indent=2))

    elif args.command == "pin-preferred-sku":
        entry = mgr.pin_preferred_sku(
            canonical_key=args.ingredient,
            product_id=args.product,
            sku_id=args.sku,
            name=args.name,
            brand=args.brand,
            package_size=args.package_size,
        )
        print(json.dumps({"status": "PINNED", "preferred_sku": entry}, indent=2))


if __name__ == "__main__":
    main()

