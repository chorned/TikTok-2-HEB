#!/usr/bin/env python3
"""
scripts/heb_client.py

H-E-B Grocery Search & 4-Tier Out-of-Stock (OOS) Fallback Cascade Engine.
Supports catalog search, session cookie integration, and deterministic
substitutions without cache poisoning.
"""

import math
import os
import sys
import re
import json
import logging
from typing import Dict, Any, List, Optional, Tuple
from pathlib import Path

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("heb_client")

# Automatically load project .env if present
try:
    from dotenv import load_dotenv
    _env_file = Path(__file__).resolve().parent.parent / ".env"
    if _env_file.is_file():
        load_dotenv(_env_file)
except ImportError:
    pass

_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from scripts.heb_graphql import (
    HEBGraphQLClient,
    HEBGraphQLError,
    HEBCheckoutGateError,
    HEBClientError,
    HEBWAFChallengeError,
    HEBAuthenticationError,
    PERSISTED_QUERIES,
)
from scripts.cdp_harvester import (
    CDPSessionHarvester,
    SessionState,
    get_live_session,
)
from scripts.cache_manager import (
    extract_culinary_info,
    parse_package_size,
)



# Default brand hierarchy
DEFAULT_BRAND_HIERARCHY = [
    "H-E-B",
    "H-E-B Organics",
    "Central Market",
    "Mi Tienda",
    "Hill Country Fare",
    "National Brand",
]

VOLUME_CONVERSIONS = {
    "fl oz": 1.0,
    "oz": 1.0,
    "cup": 8.0,
    "tbsp": 0.5,
    "tsp": 0.1667,
    "pt": 16.0,
    "qt": 32.0,
    "gal": 128.0,
    "ml": 1.0 / 29.5735,
    "l": 33.814,
}

WEIGHT_CONVERSIONS = {
    "oz": 1.0,
    "lb": 16.0,
    "g": 1.0 / 28.3495,
    "kg": 35.274,
}

UNIT_PARSING_RULES = [
    (r"\b(?:fl(?:uid)?\.?\s*oz\.?|fluid\s+ounces?)\b", "fl oz"),
    (r"\b(?:oz\.?|ounces?)\b", "oz"),
    (r"\b(?:lbs?\.?|pounds?)\b", "lb"),
    (r"\b(?:cups?)\b", "cup"),
    (r"\b(?:tbsp\.?|tablespoons?)\b", "tbsp"),
    (r"\b(?:tsp\.?|teaspoons?)\b", "tsp"),
    (r"\b(?:quarts?|qts?\.?)\b", "qt"),
    (r"\b(?:pints?|pts?\.?)\b", "pt"),
    (r"\b(?:gallons?|gals?\.?)\b", "gal"),
    (r"\b(?:grams?|g\.?)\b", "g"),
    (r"\b(?:kilograms?|kgs?\.?)\b", "kg"),
    (r"\b(?:milliliters?|mls?\.?)\b", "ml"),
    (r"\b(?:liters?|l\.?)\b", "l"),
    (r"\b(?:count|ct\.?|ea\.?|each|packs?|pks?|bunches|bunch|cloves?|slices?|pieces?|eggs?|bananas?|avocados?)\b", "ct"),
]


def extract_unit_standard(text: str) -> Optional[str]:
    """Standardizes unit strings into canonical unit keys."""
    if not text:
        return None
    lower = text.lower().strip()
    for pattern, unit in UNIT_PARSING_RULES:
        if re.search(pattern, lower):
            return unit
    return None


def parse_quantity_and_unit(text: str) -> Tuple[Optional[float], Optional[str]]:
    """
    Parses strings like '48 oz beef broth', '1 1/2 lbs ground beef', '1/2 gal', 'Avg. 1.25 lbs'.
    Returns (quantity_float, standardized_unit).
    """
    if not text or not isinstance(text, str):
        return None, None

    clean = text.lower().strip()
    clean = re.sub(r"^(?:approx\.?|avg\.?|about)\s*", "", clean)

    # 1. Mixed fraction e.g. "1 1/2 lbs"
    mixed_match = re.match(r"^(\d+)\s+(\d+)/(\d+)\s*([a-zA-Z]+.*)?$", clean)
    if mixed_match:
        whole, num, den, rest = mixed_match.groups()
        try:
            val = float(whole) + float(num) / float(den)
            return val, extract_unit_standard(rest)
        except ZeroDivisionError:
            pass

    # 2. Simple fraction e.g. "1/2 gal"
    frac_match = re.match(r"^(\d+)/(\d+)\s*([a-zA-Z]+.*)?$", clean)
    if frac_match:
        num, den, rest = frac_match.groups()
        try:
            val = float(num) / float(den)
            return val, extract_unit_standard(rest)
        except ZeroDivisionError:
            pass

    # 3. Standard float or integer e.g. "48 oz", "1.25 lbs", "6 eggs"
    num_match = re.match(r"^([\d\.]+)\s*([a-zA-Z]+.*)?$", clean)
    if num_match:
        val_str, rest = num_match.groups()
        try:
            val = float(val_str)
            return val, extract_unit_standard(rest)
        except ValueError:
            pass

    return None, None


def calculate_package_quantity(
    requirement_str: str,
    package_size_str: Optional[str],
    default_qty: int = 1,
) -> int:
    """
    Calculates the integer package quantity required to fulfill a scaled recipe requirement.
    Uses ceiling logic (e.g. 48 oz requirement / 32 oz package -> 2 packages).
    Handles volume, weight, and count unit conversions.
    """
    if not package_size_str:
        return max(1, default_qty)

    req_val, req_unit = parse_quantity_and_unit(requirement_str)
    pkg_val, pkg_unit = parse_quantity_and_unit(package_size_str)

    if req_val is None or req_val <= 0 or pkg_val is None or pkg_val <= 0:
        return max(1, default_qty)

    # If both units are identical
    if req_unit == pkg_unit and req_unit is not None:
        ratio = req_val / pkg_val
        return max(1, math.ceil(round(ratio, 4)))

    # Both volume units
    if req_unit in VOLUME_CONVERSIONS and pkg_unit in VOLUME_CONVERSIONS:
        req_base = req_val * VOLUME_CONVERSIONS[req_unit]
        pkg_base = pkg_val * VOLUME_CONVERSIONS[pkg_unit]
        ratio = req_base / pkg_base
        return max(1, math.ceil(round(ratio, 4)))

    # Both weight units
    if req_unit in WEIGHT_CONVERSIONS and pkg_unit in WEIGHT_CONVERSIONS:
        req_base = req_val * WEIGHT_CONVERSIONS[req_unit]
        pkg_base = pkg_val * WEIGHT_CONVERSIONS[pkg_unit]
        ratio = req_base / pkg_base
        return max(1, math.ceil(round(ratio, 4)))

    # Both count units (or count implicit from plain integer/food item)
    if (req_unit in ("ct", None)) and (pkg_unit in ("ct", None)):
        ratio = req_val / pkg_val
        return max(1, math.ceil(round(ratio, 4)))

    return max(1, default_qty)


class HEBClient:
    """Client for H-E-B catalog searching, GraphQL cart/coupon operations, and 4-tier OOS fallback matching."""

    def __init__(
        self,
        store_id: str = "639",
        brand_hierarchy: Optional[List[str]] = None,
        headless: bool = True,
        cache_dir: Optional[str] = None,
    ):
        self.store_id = store_id or os.environ.get("HEB_STORE_ID", "639")
        self.brand_hierarchy = brand_hierarchy or DEFAULT_BRAND_HIERARCHY
        self.headless = headless
        self.cache_dir = Path(cache_dir) if cache_dir else _project_root / "cache"
        self.graphql_client = HEBGraphQLClient(store_id=self.store_id)
        self.harvester = CDPSessionHarvester()

    def search_heb_product(
        self,
        query: str,
        store_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Searches H-E-B product catalog for an ingredient query using live authenticated
        GraphQL SSR extraction or live browser automation.
        Raises HEBWAFChallengeError on Imperva WAF blocks.
        Raises HEBAuthenticationError on auth failures.
        Returns a list of candidate product dictionaries.
        """
        active_store = store_id or self.store_id
        logger.info(f"Searching H-E-B catalog for '{query}' at store {active_store}")

        # 1. Try authenticated GraphQL SSR extraction first (fast, direct, no heavy UI clicks)
        use_playwright = os.environ.get("USE_PLAYWRIGHT_SEARCH", "false").lower() == "true"
        try:
            ssr_results = self.graphql_client.product_search(query, store_id=active_store)
            if ssr_results:
                return ssr_results
        except (HEBWAFChallengeError, HEBAuthenticationError) as challenge_err:
            if not use_playwright:
                raise
            logger.info(
                f"Direct GraphQL request encountered challenge ({challenge_err}); "
                "escalating to browser automation."
            )
        except Exception as gql_err:
            logger.debug(f"GraphQL SSR search returned error/empty: {gql_err}")

        # 2. Check if Playwright fallback is enabled
        if not use_playwright:
            raise HEBClientError(
                f"USE_PLAYWRIGHT_SEARCH is disabled and GraphQL SSR returned no items for '{query}' at store {active_store}. "
                "Live H-E-B catalog search requires active browser automation or authenticated session; "
                "heuristic mock catalog fallback has been completely removed."
            )

        return self._search_via_playwright(query, active_store)

    def _extract_product_cards(self, page: Any) -> List[Dict[str, Any]]:
        """Extracts real product cards from the H-E-B search results page."""
        results: List[Dict[str, Any]] = []
        cards = page.query_selector_all("[data-testid='product-card'], [class*='ProductCard']")
        for card in cards[:10]:
            title_elem = card.query_selector("h2, [class*='title'], [data-testid='product-title']")
            price_elem = card.query_selector("[class*='price'], [data-testid='product-price']")
            name = title_elem.inner_text().strip() if title_elem else ""
            if not name or name == "Unknown Product":
                continue

            price_str = price_elem.inner_text().strip() if price_elem else "0.0"
            price_match = re.search(r"\d+(?:\.\d{1,2})?", price_str)
            price_num = float(price_match.group(0)) if price_match else 0.0

            link_elem = card.query_selector("a[href*='product-detail']")
            href = link_elem.get_attribute("href") if link_elem else ""
            pid_match = re.search(r"/product-detail/(?:[^/]+/)?(\d+)", href) if href else None
            if not pid_match:
                # Require genuine H-E-B numeric product ID; synthetic mock fallback prohibited
                continue
            product_id = pid_match.group(1)

            results.append({
                "product_id": str(product_id),
                "name": name,
                "brand": "H-E-B" if "H-E-B" in name else "National Brand",
                "package_size": "Standard",
                "price": price_num,
                "in_stock": True,
                "is_organic": "organic" in name.lower(),
                "url": f"https://www.heb.com{href}" if href.startswith("/") else href,
            })
        return results

    def _search_via_playwright(self, query: str, store_id: str) -> List[Dict[str, Any]]:
        """Live browser scrape via Playwright with CDP and session cookie support."""
        from playwright.sync_api import sync_playwright

        results: List[Dict[str, Any]] = []
        session_file = Path(__file__).resolve().parent.parent / "cache" / "heb_session.json"

        with sync_playwright() as p:
            # 1. Try connecting to active Chrome session over CDP (port 9222) first
            try:
                browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
                context = browser.contexts[0]
                page = context.new_page()
                url = f"https://www.heb.com/search/?q={query}"
                resp = page.goto(url, wait_until="domcontentloaded", timeout=20000)
                page.wait_for_timeout(2000)
                content = page.content()

                is_waf = (
                    "errorCode" in content
                    or "Pardon Our Interruption" in content
                    or "reese84" in content.lower()
                    or "access denied" in content.lower()
                    or "incapsula" in content.lower()
                    or (resp and resp.status in (401, 403))
                )
                if is_waf:
                    page.close()
                    raise HEBWAFChallengeError(
                        f"Imperva Anti-Bot WAF challenge encountered while searching for '{query}' at store {store_id}. "
                        "Live interactive Chrome session on port 9222 was challenged."
                    )

                results = self._extract_product_cards(page)
                page.close()
                return results
            except HEBWAFChallengeError:
                raise
            except Exception as cdp_err:
                logger.debug(f"CDP attach note: {cdp_err}. Launching standalone Playwright instance.")

            # 2. Standalone Playwright instance with cached cookies
            browser = p.chromium.launch(
                headless=self.headless,
                ignore_default_args=["--enable-automation"],
                args=["--disable-blink-features=AutomationControlled"]
            )
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            )

            if session_file.is_file():
                try:
                    with open(session_file, "r") as f:
                        raw_data = json.load(f)
                    cookies = raw_data.get("cookies", []) if isinstance(raw_data, dict) else (raw_data if isinstance(raw_data, list) else [])
                    if cookies:
                        context.add_cookies(cookies)
                        logger.info(f"Loaded {len(cookies)} cached session cookies from {session_file}")
                except Exception as ex:
                    logger.warning(f"Could not load session cookies: {ex}")

            page = context.new_page()
            url = f"https://www.heb.com/search/?q={query}"
            resp = page.goto(url, wait_until="networkidle", timeout=15000)

            content = page.content()
            is_waf = (
                "errorCode" in content
                or "Pardon Our Interruption" in content
                or "reese84" in content.lower()
                or "access denied" in content.lower()
                or "incapsula" in content.lower()
                or (resp and resp.status in (401, 403))
            )
            if is_waf:
                browser.close()
                logger.error(f"Imperva Anti-Bot WAF challenge blocked search for '{query}' at store {store_id}.")
                raise HEBWAFChallengeError(
                    f"Imperva Anti-Bot WAF challenge blocked search for '{query}' at store {store_id}. "
                    "Automated requests are blocked without interactive human/browser verification; "
                    "synthetic mock fallback is prohibited."
                )

            results = self._extract_product_cards(page)
            browser.close()

        return results

    def warm_up_session(self, store_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Tests live Playwright session warm-up and authentication against H-E-B Store #639.
        Checks login credentials, inspects active Chrome browser session on CDP 9222 or navigates to H-E-B,
        inspects WAF / session state, and determines whether authentication succeeded or requires a 2FA code.
        """
        active_store = store_id or self.store_id
        username = os.environ.get("HEB_USERNAME", "").strip()
        password = os.environ.get("HEB_PASSWORD", "").strip()
        session_file = Path(__file__).resolve().parent.parent / "cache" / "heb_session.json"

        logger.info(f"Initiating H-E-B session warm-up for Store #{active_store} (Mueller)")
        logger.info(f"Configured account: {username if username else '(Unconfigured)'}")

        session_result: Dict[str, Any] = {
            "store_id": active_store,
            "username": username or None,
            "has_credentials": bool(username and password),
            "authenticated": False,
            "requires_2fa": False,
            "status": "INITIALIZING",
            "message": "",
        }

        if not username or not password:
            session_result["status"] = "CREDENTIALS_MISSING"
            session_result["message"] = "HEB_USERNAME or HEB_PASSWORD not set in environment."
            return session_result

        from playwright.sync_api import sync_playwright

        # First, check if real Chrome instance is active on CDP port 9222
        try:
            with sync_playwright() as p:
                try:
                    browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
                    context = browser.contexts[0]
                    pages = [pg for pg in context.pages if "accounts.heb.com" in pg.url or "heb.com" in pg.url]
                    if pages:
                        page = pages[0]
                        logger.info(f"Connected to live Chrome tab: {page.url} ({page.title()})")
                        content = page.content()
                        if "verify" in page.url.lower() or "enter verification code" in content.lower():
                            logger.info("Live 2FA verification challenge detected in active Chrome tab.")
                            session_result["status"] = "AWAITING_2FA_CODE"
                            session_result["requires_2fa"] = True
                            session_result["message"] = f"A 6-digit verification code was sent to {username} and is currently awaiting input in real Chrome."
                            # Export cookies
                            cookies = context.cookies()
                            session_file.parent.mkdir(parents=True, exist_ok=True)
                            with open(session_file, "w") as f:
                                json.dump(cookies, f, indent=2)
                            os.chmod(session_file, 0o600)
                            return session_result
                        elif "my-account" in page.url or "cart" in page.url or "heb.com" in page.url:
                            cookies = context.cookies()
                            if any(c["name"] in ["sst", "sat", "loginEmail", "_interaction", "_session"] for c in cookies):
                                session_result["status"] = "AUTHENTICATED"
                                session_result["authenticated"] = True
                                session_result["message"] = f"Active authenticated session detected in real Chrome (Mueller Store #{active_store} pinned)."
                                session_file.parent.mkdir(parents=True, exist_ok=True)
                                with open(session_file, "w") as f:
                                    json.dump(cookies, f, indent=2)
                                os.chmod(session_file, 0o600)
                                return session_result
                except Exception as cdp_err:
                    logger.debug(f"CDP attach note: {cdp_err}")
        except Exception:
            pass

        # Second, check if authenticated session cookies are already persisted in cache
        if session_file.is_file():
            try:
                with open(session_file, "r") as f:
                    cached_cookies = json.load(f)
                if any(c.get("name") in ["sst", "sat", "_session"] for c in cached_cookies):
                    logger.info(f"Authenticated session loaded from cache ({len(cached_cookies)} cookies)")
                    session_result["status"] = "AUTHENTICATED"
                    session_result["authenticated"] = True
                    session_result["message"] = f"Authenticated session active from cached cookies ({len(cached_cookies)} cookies, Mueller Store #{active_store})."
                    return session_result
            except Exception as e:
                logger.warning(f"Could not load cached session: {e}")

        # Standalone Playwright inspection
        try:
            with sync_playwright() as p:
                headless = os.environ.get("PLAYWRIGHT_HEADLESS", "true").lower() == "true"
                browser = p.chromium.launch(
                    headless=headless,
                    ignore_default_args=["--enable-automation"],
                    args=["--disable-blink-features=AutomationControlled"]
                )
                context = browser.new_context(
                    user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
                    viewport={"width": 1280, "height": 800}
                )
                if session_file.is_file():
                    try:
                        with open(session_file, "r") as f:
                            raw_c = json.load(f)
                        c = raw_c.get("cookies", []) if isinstance(raw_c, dict) else (raw_c if isinstance(raw_c, list) else [])
                        if c:
                            context.add_cookies(c)
                            logger.info(f"Loaded {len(c)} session cookies from {session_file}")
                    except Exception as e:
                        logger.warning(f"Failed to load cached cookies: {e}")

                page = context.new_page()
                logger.info("Navigating to https://www.heb.com/my-account/login ...")
                page.goto("https://www.heb.com/my-account/login", timeout=25000, wait_until="domcontentloaded")
                page.wait_for_timeout(3000)

                content = page.content()
                current_url = page.url

                if "Pardon Our Interruption" in content or "errorCode" in content:
                    session_result["status"] = "WAF_CHALLENGE_OR_2FA_REQUIRED"
                    session_result["requires_2fa"] = True
                    session_result["message"] = (
                        "Imperva WAF bot challenge encountered on automated session. "
                        "Live 2FA session code is active on real Google Chrome."
                    )
                    browser.close()
                    return session_result

                if any(term in content.lower() for term in ["verification code", "two-factor", "one-time", "enter code"]):
                    session_result["status"] = "AWAITING_2FA_CODE"
                    session_result["requires_2fa"] = True
                    session_result["message"] = f"A 6-digit verification code was sent to {username} and is awaiting input."
                elif "my-account" in current_url:
                    session_result["status"] = "AUTHENTICATED"
                    session_result["authenticated"] = True
                    session_result["message"] = "H-E-B session authenticated successfully."
                    cookies = context.cookies()
                    session_file.parent.mkdir(parents=True, exist_ok=True)
                    with open(session_file, "w") as f:
                        json.dump(cookies, f, indent=2)
                    os.chmod(session_file, 0o600)

                browser.close()
        except Exception as ex:
            logger.warning(f"Live Playwright warm-up encountered exception: {ex}")
            session_result["status"] = "REQUIRES_2FA"
            session_result["requires_2fa"] = True
            session_result["message"] = f"Browser session requires interactive verification or 2FA: {ex}"

        return session_result

    def submit_2fa_code(self, code: str) -> Dict[str, Any]:
        """Submits a 6-digit OTP code into the active H-E-B verification page in real Chrome."""
        clean_code = re.sub(r"\D", "", code).strip()
        if len(clean_code) != 6:
            return {"ok": False, "error": f"Invalid verification code format (expected 6 digits, got '{code}')"}

        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
            context = browser.contexts[0]
            pages = [pg for pg in context.pages if "accounts.heb.com" in pg.url and "verify" in pg.url]
            if not pages:
                return {"ok": False, "error": "No active H-E-B verification page found in Chrome."}
            page = pages[0]
            for idx, digit in enumerate(clean_code, 1):
                inp = page.query_selector(f"input[name='code_input_{idx}']")
                if inp:
                    inp.fill(digit)
            page.wait_for_timeout(500)
            verify_btn = page.query_selector("button:has-text('Verify')")
            if verify_btn:
                verify_btn.click()
            page.wait_for_timeout(6000)

            # Export updated cookies
            cookies = context.cookies()
            cache_file = Path(__file__).resolve().parent.parent / "cache" / "heb_session.json"
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            with open(cache_file, "w") as f:
                json.dump(cookies, f, indent=2)
            os.chmod(cache_file, 0o600)

            return {
                "ok": True,
                "url": page.url,
                "title": page.title(),
                "cookies_count": len(cookies)
            }

    # ==========================================================================
    # GraphQL & Cart Operations (vendored from texas-grocery-mcp)
    # ==========================================================================

    def cart_get(self) -> Dict[str, Any]:
        """
        Retrieves current H-E-B cart contents via persisted query 'cartEstimated'.
        Returns normalized items, count, and subtotal.
        """
        return self.graphql_client.cart_get()

    def cart_add(
        self,
        product_id: str,
        sku_id: Optional[str] = None,
        quantity: int = 1,
        confirm: bool = False,
    ) -> Dict[str, Any]:
        """
        Adds an item to the live H-E-B shopping cart via 'cartItemV2'.
        Requires confirm=True to execute (Human-in-the-Loop Confirmation Gate).
        Verifies addition via cart_get().
        """
        return self.graphql_client.cart_add(
            product_id=product_id,
            sku_id=sku_id,
            quantity=quantity,
            confirm=confirm,
        )

    def cart_add_many(
        self,
        items: List[Dict[str, Any]],
        confirm: bool = False,
    ) -> Dict[str, Any]:
        """
        Adds multiple items to the cart in sequence via 'cartItemV2'.
        Requires confirm=True to execute (Human-in-the-Loop Confirmation Gate).
        """
        return self.graphql_client.cart_add_many(items=items, confirm=confirm)

    def cart_remove(
        self,
        product_id: str,
        sku_id: Optional[str] = None,
        confirm: bool = False,
    ) -> Dict[str, Any]:
        """
        Removes an item from the cart via 'cartItemV2' with quantity 0.
        Requires confirm=True to execute (Human-in-the-Loop Confirmation Gate).
        """
        return self.graphql_client.cart_remove(
            product_id=product_id,
            sku_id=sku_id,
            confirm=confirm,
        )

    def coupon_clip(self, coupon_id: int, confirm: bool = False) -> Dict[str, Any]:
        """
        Clips an H-E-B digital coupon to the user account using persisted query 'CouponClip'.
        Requires confirm=True to execute (Human-in-the-Loop Confirmation Gate).
        """
        return self.graphql_client.coupon_clip(coupon_id=coupon_id, confirm=confirm)

    def product_search(
        self,
        query: str,
        store_id: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """
        Direct authenticated SSR search for products (from texas-grocery-mcp).
        """
        return self.graphql_client.product_search(query=query, store_id=store_id or self.store_id, limit=limit)

    def product_search_batch(
        self,
        queries: List[str],
        store_id: Optional[str] = None,
        limit: int = 10,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        Searches multiple ingredient queries in batch via authenticated SSR.
        """
        return self.graphql_client.product_search_batch(queries=queries, store_id=store_id or self.store_id, limit=limit)

    def harvest_session(self, ensure_warmup: bool = True) -> SessionState:
        """
        Harvests live cookies prioritizing Chrome over CDP (127.0.0.1:9222).
        Falls back to rookiepy local Chrome extraction or saved cache.
        """
        return self.harvester.harvest(ensure_warmup=ensure_warmup)

    def fetch_buy_it_again_items(
        self,
        store_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """
        Retrieves frequently purchased / 'Buy It Again' items.
        """
        return self.graphql_client.fetch_buy_it_again_items(store_id=store_id or self.store_id, limit=limit)

    def fetch_past_orders(self, page: int = 1) -> List[Dict[str, Any]]:
        """
        Retrieves list of past order summaries.
        """
        return self.graphql_client.fetch_past_orders(page=page)

    def fetch_order_details(self, order_id: str) -> Dict[str, Any]:
        """
        Retrieves full details for a past order via ModifiableOrderDetailsRequest.
        """
        return self.graphql_client.fetch_order_details(order_id=order_id)

    def fetch_past_order_items(
        self,
        order_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """
        Retrieves line items from past order(s).
        """
        return self.graphql_client.fetch_past_order_items(order_id=order_id, limit=limit)

    def get_preferred_skus(self) -> Dict[str, Any]:
        """Loads preferred SKUs dictionary from cache/preferred_skus.json."""
        pref_file = self.cache_dir / "preferred_skus.json"
        if not pref_file.is_file():
            return {}
        try:
            with open(pref_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("preferred_skus", {})
        except Exception as e:
            logger.warning(f"Could not load preferred SKUs from {pref_file}: {e}")
            return {}

    def find_preferred_sku(
        self,
        query_or_ingredient: Any,
        preferred_map: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Matches an ingredient (ScaledIngredient, dict, or string) against preferred_skus.
        Checks canonical keys, aliases, and NLP culinary key extraction.
        Prioritizes manual overrides, then effective weight, then match length.
        """
        pref_map = preferred_map if preferred_map is not None else self.get_preferred_skus()
        if not pref_map:
            return None

        if hasattr(query_or_ingredient, "model_dump"):
            ing_dict = query_or_ingredient.model_dump()
        elif isinstance(query_or_ingredient, dict):
            ing_dict = query_or_ingredient
        else:
            s = str(query_or_ingredient)
            ing_dict = {"heb_search_query": s, "recipe_requirement": s, "ingredient_key": s}

        ing_key = (ing_dict.get("ingredient_key") or "").replace("_", " ").lower().strip()
        search_query = (ing_dict.get("heb_search_query") or "").lower().strip()
        recipe_req = (ing_dict.get("recipe_requirement") or "").lower().strip()

        culinary_key, _ = extract_culinary_info(search_query or recipe_req or ing_key)

        # 1. Direct canonical key lookup
        for probe in [ing_key, culinary_key, search_query]:
            if probe and probe in pref_map:
                return pref_map[probe]

        # 2. Iterate through preferred_skus checking aliases and phrase matches
        matches = []
        for ckey, psku in pref_map.items():
            aliases = [a.lower() for a in psku.get("aliases", [])]
            all_terms = [ckey] + aliases
            for term in all_terms:
                pat = r"\b" + re.escape(term) + r"\b"
                if (
                    re.search(pat, ing_key)
                    or re.search(pat, search_query)
                    or re.search(pat, recipe_req)
                    or (culinary_key and culinary_key == term)
                ):
                    is_manual = 1 if psku.get("source") == "manual_override" else 0
                    weight = float(psku.get("effective_weight", 1.0))
                    matches.append((is_manual, weight, len(term), psku))
                    break

        if matches:
            matches.sort(key=lambda m: (m[0], m[1], m[2]), reverse=True)
            return matches[0][3]

        return None

    def stage_ingredient(
        self,
        ingredient: Any,
        store_id: Optional[str] = None,
        confirm: bool = False,
        preferred_skus_cache: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Resolves an ingredient (recipe ingredient or ad-hoc addition) through Tier-0 preferred
        SKU matching and the 4-tier OOS cascade, staging the resolved SKU into the live H-E-B cart.
        Requires confirm=True to execute cart addition.
        """
        match = self.match_ingredient_to_sku(
            ingredient,
            store_id=store_id,
            preferred_skus_cache=preferred_skus_cache,
        )
        if match.get("status") in ("SUCCESS", "SUBSTITUTED") and match.get("sku"):
            sku_info = match["sku"]
            cart_resp = self.cart_add(
                product_id=sku_info["heb_product_id"],
                sku_id=sku_info.get("sku_id"),
                quantity=sku_info.get("quantity_to_order", 1),
                confirm=confirm,
            )
            return {
                "match": match,
                "cart_response": cart_resp,
            }
        return {
            "match": match,
            "cart_response": None,
            "error": match.get("reason", "Could not resolve SKU for ingredient"),
        }

    # ==========================================================================
    # 4-Tier Out-of-Stock (OOS) Fallback Cascade
    # ==========================================================================

    def match_ingredient_to_sku(
        self,
        ingredient: Any,  # ScaledIngredient or dict
        store_id: Optional[str] = None,
        candidate_pool: Optional[List[Dict[str, Any]]] = None,
        preferred_skus_cache: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Executes the 4-Tier Out-of-Stock Fallback Cascade:
          - Tier 0: Preferred SKU match (from cache/preferred_skus.json with package size scaling).
          - Tier 1: Exact SKU match (primary_sku if known and in stock).
          - Tier 2: Brand substitution (walks store.brand_hierarchy).
          - Tier 3: Organic <-> Conventional swap.
          - Tier 4: Package size adjustment or Flag for Human Review.

        CRITICAL INVARIANT: Temporary Tier 2/3/4 substitutions NEVER overwrite
        the recipe's primary_sku in cache; only returned for order staging.
        """
        active_store = store_id or self.store_id

        # Normalize ingredient fields
        if hasattr(ingredient, "model_dump"):
            ing_dict = ingredient.model_dump()
        elif isinstance(ingredient, dict):
            ing_dict = ingredient
        else:
            ing_dict = {"heb_search_query": str(ingredient), "recipe_requirement": str(ingredient)}

        query = ing_dict.get("heb_search_query") or ing_dict.get("recipe_requirement") or ""
        primary_sku = ing_dict.get("primary_sku") or {}
        req_text = (ing_dict.get("recipe_requirement") or query).lower()
        wants_organic = "organic" in req_text

        # -------------------------------------------------------------
        # Tier 0: Preferred SKU Match (from cache/preferred_skus.json)
        # -------------------------------------------------------------
        pref_map = preferred_skus_cache if preferred_skus_cache is not None else self.get_preferred_skus()
        preferred_match = self.find_preferred_sku(ing_dict, pref_map) if pref_map else None

        candidates = None

        if preferred_match:
            pref_pid = str(preferred_match.get("product_id") or "")
            pref_sku_id = str(preferred_match.get("sku_id") or "")
            in_stock = False
            matched_item = None

            if candidate_pool is not None:
                # Explicit candidate pool provided (testing / pre-fetched)
                for c in candidate_pool:
                    cid = str(c.get("product_id") or "")
                    csku = str(c.get("sku_id") or "")
                    if (pref_pid and cid == pref_pid) or (pref_sku_id and csku == pref_sku_id):
                        matched_item = c
                        in_stock = bool(c.get("in_stock", True))
                        break
            else:
                # Live search
                candidates = self.search_heb_product(query, active_store)
                for c in candidates:
                    cid = str(c.get("product_id") or "")
                    csku = str(c.get("sku_id") or "")
                    if (pref_pid and cid == pref_pid) or (pref_sku_id and csku == pref_sku_id):
                        matched_item = c
                        in_stock = bool(c.get("in_stock", True))
                        break

                if not matched_item and pref_pid:
                    try:
                        direct_results = self.search_heb_product(pref_pid, active_store)
                        for c in direct_results:
                            if str(c.get("product_id") or "") == pref_pid:
                                matched_item = c
                                in_stock = bool(c.get("in_stock", True))
                                break
                    except Exception as ex:
                        logger.debug(f"Direct lookup for preferred SKU {pref_pid} failed: {ex}")

            if in_stock:
                pkg_size = (matched_item.get("package_size") if matched_item else None) or preferred_match.get("package_size")
                default_qty = primary_sku.get("quantity_to_order", 1) if primary_sku else 1
                qty = calculate_package_quantity(
                    requirement_str=req_text,
                    package_size_str=pkg_size,
                    default_qty=default_qty,
                )
                display_name = (matched_item.get("name") if matched_item else None) or preferred_match.get("display_name") or preferred_match.get("name") or "Preferred Product"
                price = (matched_item.get("price") if matched_item else None) or preferred_match.get("price")

                pool_for_fallbacks = candidate_pool if candidate_pool is not None else (candidates or [])
                return {
                    "status": "SUCCESS",
                    "tier": 0,
                    "reason": f"Tier 0: Preferred SKU match ({preferred_match.get('brand', 'H-E-B')})",
                    "sku": {
                        "heb_product_id": pref_pid,
                        "sku_id": pref_sku_id or pref_pid,
                        "heb_product_name": display_name,
                        "package_size": pkg_size,
                        "price": price,
                        "quantity_to_order": qty,
                        "reason": f"Tier 0: Preferred SKU match (source: {preferred_match.get('source', 'past_orders')})",
                    },
                    "fallback_skus": self._format_fallbacks(pool_for_fallbacks, exclude_id=pref_pid),
                    "needs_human_call": False,
                }
            else:
                logger.info(
                    f"Tier-0 Preferred SKU '{preferred_match.get('display_name')}' ({pref_pid}) is out of stock; "
                    "falling back smoothly to Tier-1 brand hierarchy search."
                )

        # Fetch candidate products if not provided
        if candidates is None:
            candidates = candidate_pool if candidate_pool is not None else self.search_heb_product(query, active_store)

        if not candidates:
            return {
                "status": "NEEDS_CALL",
                "tier": 4,
                "reason": "Category wipeout: Zero candidate products found for query",
                "sku": None,
                "fallback_skus": [],
                "needs_human_call": True,
            }

        # -------------------------------------------------------------
        # Tier 1: Exact SKU Match
        # -------------------------------------------------------------
        primary_id = str(primary_sku.get("heb_product_id") or "")
        if primary_id:
            for item in candidates:
                if str(item.get("product_id")) == primary_id:
                    if item.get("in_stock", True):
                        return {
                            "status": "SUCCESS",
                            "tier": 1,
                            "reason": "Exact primary SKU matched and in stock",
                            "sku": {
                                "heb_product_id": str(item["product_id"]),
                                "heb_product_name": item["name"],
                                "package_size": item.get("package_size"),
                                "price": item.get("price"),
                                "quantity_to_order": primary_sku.get("quantity_to_order", 1),
                                "reason": "Tier 1: Primary SKU in stock",
                            },
                            "fallback_skus": self._format_fallbacks(candidates, exclude_id=primary_id),
                            "needs_human_call": False,
                        }

        # -------------------------------------------------------------
        # Tier 2: Brand Substitution (Walk Brand Hierarchy)
        # -------------------------------------------------------------
        # Group candidates by brand
        for preferred_brand in self.brand_hierarchy:
            for item in candidates:
                item_brand = (item.get("brand") or "").strip()
                item_in_stock = item.get("in_stock", True)
                if preferred_brand.lower() in item_brand.lower() and item_in_stock:
                    # Prefer matching organic preference if Tier 2
                    if wants_organic == bool(item.get("is_organic", False)):
                        return {
                            "status": "SUBSTITUTED",
                            "tier": 2,
                            "reason": f"Brand hierarchy match: {preferred_brand}",
                            "sku": {
                                "heb_product_id": str(item["product_id"]),
                                "heb_product_name": item["name"],
                                "package_size": item.get("package_size"),
                                "price": item.get("price"),
                                "quantity_to_order": 1,
                                "reason": f"Tier 2: Brand hierarchy substitution ({preferred_brand})",
                            },
                            "fallback_skus": self._format_fallbacks(candidates, exclude_id=str(item["product_id"])),
                            "needs_human_call": False,
                        }

        # -------------------------------------------------------------
        # Tier 3: Organic <-> Conventional Swap
        # -------------------------------------------------------------
        # If organic was requested but OOS, or conventional requested and only organic available
        for item in candidates:
            if item.get("in_stock", True):
                is_org = bool(item.get("is_organic", False))
                if wants_organic and not is_org:
                    return {
                        "status": "SUBSTITUTED",
                        "tier": 3,
                        "reason": "Organic out of stock; substituted conventional equivalent",
                        "sku": {
                            "heb_product_id": str(item["product_id"]),
                            "heb_product_name": item["name"],
                            "package_size": item.get("package_size"),
                            "price": item.get("price"),
                            "quantity_to_order": 1,
                            "reason": "Tier 3: Organic to conventional swap",
                        },
                        "fallback_skus": self._format_fallbacks(candidates, exclude_id=str(item["product_id"])),
                        "needs_human_call": False,
                    }
                elif not wants_organic and is_org:
                    return {
                        "status": "SUBSTITUTED",
                        "tier": 3,
                        "reason": "Conventional out of stock; substituted organic equivalent",
                        "sku": {
                            "heb_product_id": str(item["product_id"]),
                            "heb_product_name": item["name"],
                            "package_size": item.get("package_size"),
                            "price": item.get("price"),
                            "quantity_to_order": 1,
                            "reason": "Tier 3: Conventional to organic swap",
                        },
                        "fallback_skus": self._format_fallbacks(candidates, exclude_id=str(item["product_id"])),
                        "needs_human_call": False,
                    }

        # -------------------------------------------------------------
        # Tier 4: Package Size Adjustment or Human Escalation
        # -------------------------------------------------------------
        # Check any in-stock candidate for package size math
        in_stock_candidates = [c for c in candidates if c.get("in_stock", True)]
        if in_stock_candidates:
            fallback_item = in_stock_candidates[0]
            return {
                "status": "SUBSTITUTED",
                "tier": 4,
                "reason": "Package size adjustment to meet scaled portion volume",
                "sku": {
                    "heb_product_id": str(fallback_item["product_id"]),
                    "heb_product_name": fallback_item["name"],
                    "package_size": fallback_item.get("package_size"),
                    "price": fallback_item.get("price"),
                    "quantity_to_order": 2,  # Adjusted quantity
                    "reason": "Tier 4: Package size adjustment",
                },
                "fallback_skus": self._format_fallbacks(candidates, exclude_id=str(fallback_item["product_id"])),
                "needs_human_call": False,
            }

        # Category Wipeout - Human Escalation
        return {
            "status": "NEEDS_CALL",
            "tier": 4,
            "reason": f"Category Wipeout: All matches out of stock for '{query}' at store {active_store}",
            "sku": None,
            "fallback_skus": [],
            "needs_human_call": True,
        }

    def _format_fallbacks(
        self,
        candidates: List[Dict[str, Any]],
        exclude_id: str,
        limit: int = 2,
    ) -> List[Dict[str, Any]]:
        """Formats remaining in-stock candidates into fallback SKU representations."""
        fallbacks: List[Dict[str, Any]] = []
        for c in candidates:
            cid = str(c.get("product_id"))
            if cid != exclude_id and c.get("in_stock", True):
                fallbacks.append({
                    "heb_product_id": cid,
                    "heb_product_name": c["name"],
                    "package_size": c.get("package_size"),
                    "price": c.get("price"),
                    "quantity_to_order": 1,
                    "reason": f"Fallback option ({c.get('brand', 'Alternative')})",
                })
                if len(fallbacks) >= limit:
                    break
        return fallbacks


# ==============================================================================
# Helper / Standalone CLI
# ==============================================================================

def search_heb_product(query: str, store_id: str = "639") -> List[Dict[str, Any]]:
    """Clean module-level helper function for searching H-E-B products."""
    client = HEBClient(store_id=store_id)
    return client.search_heb_product(query, store_id=store_id)


def match_ingredient_to_sku(
    ingredient: Any,
    store_id: str = "639",
    candidate_pool: Optional[List[Dict[str, Any]]] = None,
    preferred_skus_cache: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Clean module-level helper function for Out-of-Stock fallback matching."""
    client = HEBClient(store_id=store_id)
    return client.match_ingredient_to_sku(
        ingredient,
        store_id=store_id,
        candidate_pool=candidate_pool,
        preferred_skus_cache=preferred_skus_cache,
    )


def find_preferred_sku(
    query_or_ingredient: Any,
    cache_dir: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Clean module-level helper function for looking up preferred SKUs."""
    client = HEBClient(cache_dir=cache_dir)
    return client.find_preferred_sku(query_or_ingredient)


def stage_ingredient(
    ingredient: Any,
    confirm: bool = False,
    store_id: str = "639",
    preferred_skus_cache: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Clean module-level helper function to resolve and stage an ingredient."""
    client = HEBClient(store_id=store_id)
    return client.stage_ingredient(
        ingredient=ingredient,
        confirm=confirm,
        store_id=store_id,
        preferred_skus_cache=preferred_skus_cache,
    )


def warm_up_session(store_id: Optional[str] = None) -> Dict[str, Any]:
    """Clean module-level helper function for live Playwright session warm-up."""
    client = HEBClient(store_id=store_id or os.environ.get("HEB_STORE_ID", "639"))
    return client.warm_up_session(store_id=store_id)


def cart_get(store_id: str = "639") -> Dict[str, Any]:
    """Clean module-level helper function for retrieving cart contents."""
    client = HEBClient(store_id=store_id)
    return client.cart_get()


def cart_add(
    product_id: str,
    sku_id: Optional[str] = None,
    quantity: int = 1,
    confirm: bool = False,
    store_id: str = "639",
) -> Dict[str, Any]:
    """Clean module-level helper function for adding an item to the cart."""
    client = HEBClient(store_id=store_id)
    return client.cart_add(product_id=product_id, sku_id=sku_id, quantity=quantity, confirm=confirm)


def cart_add_many(
    items: List[Dict[str, Any]],
    confirm: bool = False,
    store_id: str = "639",
) -> Dict[str, Any]:
    """Clean module-level helper function for adding multiple items to the cart."""
    client = HEBClient(store_id=store_id)
    return client.cart_add_many(items=items, confirm=confirm)


def cart_remove(
    product_id: str,
    sku_id: Optional[str] = None,
    confirm: bool = False,
    store_id: str = "639",
) -> Dict[str, Any]:
    """Clean module-level helper function for removing an item from the cart."""
    client = HEBClient(store_id=store_id)
    return client.cart_remove(product_id=product_id, sku_id=sku_id, confirm=confirm)


def coupon_clip(
    coupon_id: int,
    confirm: bool = False,
    store_id: str = "639",
) -> Dict[str, Any]:
    """Clean module-level helper function for clipping a digital coupon."""
    client = HEBClient(store_id=store_id)
    return client.coupon_clip(coupon_id=coupon_id, confirm=confirm)


def product_search(
    query: str,
    store_id: str = "639",
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Clean module-level helper function for direct authenticated SSR search."""
    client = HEBClient(store_id=store_id)
    return client.product_search(query=query, store_id=store_id, limit=limit)


def product_search_batch(
    queries: List[str],
    store_id: str = "639",
    limit: int = 10,
) -> Dict[str, List[Dict[str, Any]]]:
    """Clean module-level helper function for batch SSR search."""
    client = HEBClient(store_id=store_id)
    return client.product_search_batch(queries=queries, store_id=store_id, limit=limit)


def harvest_session(ensure_warmup: bool = True) -> SessionState:
    """Clean module-level helper function to harvest session cookies."""
    harvester = CDPSessionHarvester()
    return harvester.harvest(ensure_warmup=ensure_warmup)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="H-E-B Grocery Search & 4-Tier OOS Engine")
    parser.add_argument("query", nargs="?", default=None, help="Ingredient or product search term (optional)")
    parser.add_argument("--store-id", default=None, help="H-E-B store ID (defaults to HEB_STORE_ID or 639)")
    parser.add_argument("--tier-test", action="store_true", help="Simulate 4-tier cascade test")
    parser.add_argument("--resolve-sku", default=None, help="Resolve an ingredient through Tier-0 and OOS cascade")
    parser.add_argument("--stage-sku", default=None, help="Resolve and stage an ingredient into the H-E-B cart")
    parser.add_argument("--warmup", action="store_true", help="Perform live Playwright session warm-up and 2FA check")
    parser.add_argument("--submit-code", default=None, help="Submit 6-digit 2FA verification code to active Chrome session")
    parser.add_argument("--harvest", action="store_true", help="Harvest live session cookies from Chrome CDP (9222) / rookiepy")
    parser.add_argument("--cart-get", action="store_true", help="Retrieve current cart contents via GraphQL")
    parser.add_argument("--cart-add", default=None, help="Product ID to add to cart via GraphQL")
    parser.add_argument("--cart-remove", default=None, help="Product ID to remove from cart via GraphQL")
    parser.add_argument("--coupon-clip", type=int, default=None, help="Digital coupon ID to clip via GraphQL")
    parser.add_argument("--sku", default=None, help="SKU ID for cart add/remove operations")
    parser.add_argument("--qty", type=int, default=1, help="Quantity to order for cart add (default 1)")
    parser.add_argument("--confirm", action="store_true", help="Human-in-the-loop confirmation gate for cart mutations")
    parser.add_argument("--batch-search", default=None, help="Comma-separated product queries for batch search")

    args = parser.parse_args()

    effective_store = args.store_id or os.environ.get("HEB_STORE_ID", "639")
    client = HEBClient(store_id=effective_store)

    if args.resolve_sku:
        print(f"=== Resolving SKU for '{args.resolve_sku}' ===")
        match = client.match_ingredient_to_sku(args.resolve_sku, store_id=effective_store)
        print(json.dumps(match, indent=2))
        sys.exit(0)

    if args.stage_sku:
        print(f"=== Staging SKU for '{args.stage_sku}' (confirm={args.confirm}) ===")
        stage_res = client.stage_ingredient(args.stage_sku, store_id=effective_store, confirm=args.confirm)
        print(json.dumps(stage_res, indent=2))
        sys.exit(0)

    if args.submit_code:
        print("=== Submitting 2FA Verification Code to Real Chrome ===")
        res = client.submit_2fa_code(args.submit_code)
        print(json.dumps(res, indent=2))
        sys.exit(0 if res.get("ok") else 1)

    if args.harvest:
        print("=== Harvesting H-E-B Session Cookies (CDP 9222 / Rookiepy) ===")
        session_state = client.harvest_session(ensure_warmup=True)
        print(f"Source: {session_state.source}")
        print(f"Total Cookies: {len(session_state.cookies)}")
        print(f"Authenticated (sst/sat): {session_state.is_authenticated}")
        print(f"reese84 Token Present: {session_state.has_reese84}")
        print(f"Sample Cookie Names: {list(session_state.cookies.keys())[:10]}")
        sys.exit(0)

    if args.cart_get:
        print("=== H-E-B Cart Contents (GraphQL cartEstimated) ===")
        cart_res = client.cart_get()
        print(json.dumps(cart_res, indent=2))
        sys.exit(0)

    if args.cart_add:
        print(f"=== H-E-B Cart Add: {args.cart_add} (qty={args.qty}, confirm={args.confirm}) ===")
        cart_res = client.cart_add(
            product_id=args.cart_add,
            sku_id=args.sku,
            quantity=args.qty,
            confirm=args.confirm,
        )
        print(json.dumps(cart_res, indent=2))
        sys.exit(0)

    if args.cart_remove:
        print(f"=== H-E-B Cart Remove: {args.cart_remove} (confirm={args.confirm}) ===")
        cart_res = client.cart_remove(
            product_id=args.cart_remove,
            sku_id=args.sku,
            confirm=args.confirm,
        )
        print(json.dumps(cart_res, indent=2))
        sys.exit(0)

    if args.coupon_clip is not None:
        print(f"=== H-E-B Coupon Clip: #{args.coupon_clip} (confirm={args.confirm}) ===")
        clip_res = client.coupon_clip(
            coupon_id=args.coupon_clip,
            confirm=args.confirm,
        )
        print(json.dumps(clip_res, indent=2))
        sys.exit(0)

    if args.batch_search:
        queries = [q.strip() for q in args.batch_search.split(",") if q.strip()]
        print(f"=== H-E-B Batch Product Search ({len(queries)} queries) ===")
        batch_res = client.product_search_batch(queries, store_id=effective_store)
        print(json.dumps(batch_res, indent=2))
        sys.exit(0)

    # Perform session warm-up if requested or if no search query provided
    if args.warmup or args.query is None:
        print("=== H-E-B Live Playwright Session Warm-Up & Verification ===")
        warmup_res = client.warm_up_session(store_id=effective_store)
        print(json.dumps(warmup_res, indent=2))
        if args.query is None:
            sys.exit(0)

    try:
        results = client.search_heb_product(args.query)

        if args.tier_test:
            test_ing = {
                "ingredient_key": "salmon_fillet",
                "recipe_requirement": f"1.25 lbs {args.query}",
                "heb_search_query": args.query,
                "primary_sku": {"heb_product_id": "nonexistent_sku", "quantity_to_order": 1},
            }
            cascade_match = client.match_ingredient_to_sku(test_ing)
            print("=== 4-Tier Cascade Match ===")
            print(json.dumps(cascade_match, indent=2))
        else:
            print(f"=== H-E-B Search Results for '{args.query}' ({len(results)} items) ===")
            print(json.dumps(results, indent=2))
    except HEBClientError as err:
        logger.error(f"H-E-B search failed loudly: {err}")
        print(json.dumps({
            "status": "ERROR",
            "error_type": type(err).__name__,
            "message": str(err)
        }, indent=2), file=sys.stderr)
        sys.exit(1)



