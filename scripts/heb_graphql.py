#!/usr/bin/env python3
"""
scripts/heb_graphql.py

Direct H-E-B GraphQL Engine & Operations vendored from texas-grocery-mcp.
Supports authenticated operations:
  - product_search / product_search_batch (via authenticated SSR __NEXT_DATA__)
  - cart_get (persisted query: cartEstimated)
  - cart_add / cart_add_many with human-in-the-loop confirm gate (persisted query: cartItemV2)
  - cart_remove (persisted query: cartItemV2 with quantity 0)
  - coupon_clip with confirm gate (persisted query: CouponClip)

STRICT SAFETY INVARIANT: NEVER implement or execute payment, checkout,
or delivery/pickup booking mutations. Only cart staging and coupon clipping.
"""

import os
import re
import sys
import json
import logging
from typing import Dict, Any, List, Optional, Tuple
from pathlib import Path

_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from scripts.cdp_harvester import CDPSessionHarvester, SessionState, get_live_session

logger = logging.getLogger("heb_graphql")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# Persisted Query Hashes discovered from H-E-B's frontend (via texas-grocery-mcp)
PERSISTED_QUERIES = {
    "cartEstimated": "7b033abaf2caa80bc49541e51d2b89e3cc6a316e37c4bd576d9b5c498a51e9c5",
    "cartItemV2": "ade8ec1365c185244d42f9cc4c13997fec4b633ac3c38ff39558df92b210c6d0",
    "CouponClip": "88b18ac22cee98372428d9a91d759ffb5e919026ee61c747f9f88d11336b846b",
    "SelectPickupFulfillment": "8fa3c683ee37ad1bab9ce22b99bd34315b2a89cfc56208d63ba9efc0c49a6323",
    "StoreSearch": "e01fa39e66c3a2c7881322bc48af6a5af97d49b1442d433f2d09d273de2db4b6",
    "typeaheadContent": "1ed956c0f10efcfc375321f33c40964bc236fff1397a4e86b7b53cb3b18ad329",
}

HEB_GRAPHQL_ENDPOINT = "https://www.heb.com/graphql"

# Strict safety blocklist: any checkout, payment, or order submission is strictly prohibited.
FORBIDDEN_OPERATIONS = {
    "checkout", "place_order", "submit_order", "finalize_order",
    "pay", "submit_payment", "book_delivery", "book_pickup_window"
}


class HEBClientError(Exception):
    """Base exception for all H-E-B client operations."""
    pass


class HEBGraphQLError(HEBClientError):
    """Raised when an H-E-B GraphQL operation returns an error."""
    pass


class HEBWAFChallengeError(HEBClientError):
    """Raised when Imperva Anti-Bot WAF intercepts requests."""
    pass


class HEBAuthenticationError(HEBClientError):
    """Raised when session cookies/credentials are missing or expired."""
    pass


class HEBCheckoutGateError(HEBClientError):
    """Raised if an attempt is made to execute checkout/payment."""
    pass


class HEBGraphQLClient:
    """Client for H-E-B GraphQL API using harvested session cookies."""

    def __init__(self, endpoint: str = HEB_GRAPHQL_ENDPOINT, store_id: str = "639"):
        self.endpoint = endpoint
        self.store_id = store_id or os.environ.get("HEB_STORE_ID", "639")
        self.harvester = CDPSessionHarvester()
        self._session_state: Optional[SessionState] = None

    def get_session(self, force_refresh: bool = False) -> SessionState:
        """Retrieves active session state using the CDP/Rookiepy harvester."""
        if self._session_state is None or force_refresh:
            self._session_state = self.harvester.harvest()
        return self._session_state

    def execute_persisted_query(
        self,
        operation_name: str,
        variables: Dict[str, Any],
        session_state: Optional[SessionState] = None,
    ) -> Dict[str, Any]:
        """
        Executes a persisted GraphQL query hash against H-E-B.
        Raises HEBCheckoutGateError if a forbidden mutation is requested.
        Enforces Iron Law of External Integrations for expired sat authentication.
        """
        # Strict Safety Invariant Gate
        if operation_name.lower() in FORBIDDEN_OPERATIONS or any(f in operation_name.lower() for f in ["pay", "checkout", "placeorder"]):
            raise HEBCheckoutGateError(
                f"STRICT SAFETY VIOLATION: Execution of operation '{operation_name}' is forbidden by the "
                "Human-in-the-Loop Checkout Gate. AGY is strictly prohibited from finalizing checkout or payment."
            )

        if operation_name not in PERSISTED_QUERIES:
            raise ValueError(f"Unknown persisted query operation: '{operation_name}'")

        state = session_state or self.get_session()

        # Check authentication requirements for user-scoped operations
        auth_operations = {"cartEstimated", "cartItemV2", "CouponClip", "SelectPickupFulfillment"}
        if operation_name in auth_operations:
            if state.has_sat and not state.is_sat_valid:
                raise HEBAuthenticationError(
                    f"H-E-B Session Access Token ('sat') expired at timestamp {state.sat_expires_at}. "
                    "An active human login session is required at https://www.heb.com/my-account/login. "
                    "Under the Iron Law of External Integrations, execution halted at authentication boundary."
                )
            if not state.is_authenticated and not state.has_sat and not state.has_sst:
                raise HEBAuthenticationError(
                    "H-E-B authentication session cookies ('sat', 'sst') are missing. "
                    "Login required at https://www.heb.com/my-account/login."
                )
            # If reese84 token is expired/missing, attempt 5-second Playwright warm-up
            if not state.has_valid_reese84:
                try:
                    refreshed = self.harvester.refresh_session_with_playwright(headless=True, timeout=20000, warmup_seconds=5.0)
                    if refreshed and refreshed.source != "playwright_failed":
                        state = refreshed
                        self._session_state = refreshed
                except Exception as ref_err:
                    logger.debug(f"Playwright reese84 refresh note: {ref_err}")

        hash_val = PERSISTED_QUERIES[operation_name]
        payload = {
            "operationName": operation_name,
            "variables": variables,
            "extensions": {
                "persistedQuery": {
                    "version": 1,
                    "sha256Hash": hash_val,
                }
            },
        }

        # Strategy 1: If CDP page is active, execute directly in the real Chrome context
        if state.cdp_page:
            try:
                res = state.cdp_page.evaluate(
                    """async ({ url, payload }) => {
                        const resp = await fetch(url, {
                            method: "POST",
                            headers: {
                                "Content-Type": "application/json",
                                "Accept": "application/json, text/plain, */*"
                            },
                            body: JSON.stringify(payload),
                            credentials: "include"
                        });
                        return {
                            status: resp.status,
                            data: await resp.json()
                        };
                    }""",
                    {"url": self.endpoint, "payload": payload},
                )
                if res.get("status") == 200:
                    data = res.get("data", {})
                    if "errors" in data and data["errors"]:
                        raise HEBGraphQLError(f"GraphQL returned errors: {data['errors']}")
                    return data.get("data", data)
            except Exception as ex:
                logger.debug(f"CDP in-page fetch fallback note: {ex}")

        # Strategy 2: Execute via curl_cffi with browser TLS impersonation (or httpx fallback)
        headers = {
            "User-Agent": state.user_agent,
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://www.heb.com",
            "Referer": "https://www.heb.com/cart",
            "Content-Type": "application/json",
        }

        try:
            try:
                from curl_cffi import requests as cffi_requests
                resp = cffi_requests.post(
                    self.endpoint,
                    json=payload,
                    headers=headers,
                    cookies=state.cookies,
                    impersonate="chrome120",
                    timeout=15,
                )
                status_code = resp.status_code
                resp_text = resp.text
                resp_json = resp.json
            except ImportError:
                import httpx
                h_resp = httpx.post(
                    self.endpoint,
                    json=payload,
                    headers=headers,
                    cookies=state.cookies,
                    timeout=15.0,
                )
                status_code = h_resp.status_code
                resp_text = h_resp.text
                resp_json = h_resp.json

            if status_code == 200:
                data = resp_json()
                if "errors" in data and data["errors"]:
                    err_text = str(data["errors"]).lower()
                    if "auth" in err_text or "unauthorized" in err_text:
                        raise HEBAuthenticationError(f"H-E-B GraphQL authentication failure: {data['errors']}")
                    raise HEBGraphQLError(f"GraphQL returned errors: {data['errors']}")
                return data.get("data", data)
            elif status_code in (401, 403) or "errorCode" in resp_text or "Pardon Our Interruption" in resp_text:
                if "errorCode" in resp_text or "Pardon Our Interruption" in resp_text:
                    raise HEBWAFChallengeError(f"Imperva Anti-Bot WAF challenge intercepted request (HTTP {status_code}): {resp_text[:200]}")
                if status_code == 401 or "unauthorized" in resp_text.lower():
                    raise HEBAuthenticationError(f"H-E-B authentication error (HTTP {status_code}): session expired or missing.")
                raise HEBWAFChallengeError(f"Imperva Anti-Bot WAF challenge intercepted request (HTTP {status_code}): {resp_text[:200]}")
            else:
                raise HEBGraphQLError(f"H-E-B GraphQL request returned HTTP status {status_code}: {resp_text[:200]}")

        except Exception as e:
            if isinstance(e, (HEBClientError, HEBCheckoutGateError)):
                raise
            raise HEBGraphQLError(f"Failed to execute GraphQL operation '{operation_name}': {e}")

    # ==========================================================================
    # High-Level Operations (from texas-grocery-mcp)
    # ==========================================================================

    def cart_get(self) -> Dict[str, Any]:
        """
        Retrieves current cart contents via persisted query 'cartEstimated'.
        Returns normalized items, count, and subtotal.
        """
        state = self.get_session()
        data = self.execute_persisted_query("cartEstimated", {"userIsLoggedIn": True}, session_state=state)

        cart_v2 = data.get("cartV2", {}) or data.get("cartEstimated", {})
        raw_items = cart_v2.get("items", [])
        normalized_items = []

        subtotal = 0.0
        for item in raw_items:
            product = item.get("product", {}) or {}
            sku_obj = item.get("sku", {}) or {}
            skus = product.get("SKUs", []) or []
            first_sku = skus[0] if skus else {}

            product_id = str(product.get("id") or item.get("productId") or "")
            sku_id = str(sku_obj.get("id") or item.get("skuId") or first_sku.get("id") or product_id)
            name = product.get("fullDisplayName") or product.get("displayName") or item.get("name") or "Item"
            qty = item.get("quantity", 1)

            price = 0.0
            price_obj = item.get("price", {}) or {}
            if isinstance(price_obj, dict) and price_obj.get("amount") is not None:
                price = float(price_obj["amount"])
            elif item.get("unitPrice") is not None:
                price = float(item["unitPrice"])

            subtotal += price * qty
            normalized_items.append({
                "product_id": product_id,
                "sku_id": sku_id,
                "name": name,
                "quantity": qty,
                "price": price,
                "package_size": first_sku.get("customerFriendlySize") or item.get("packageSize"),
            })

        return {
            "status": "SUCCESS",
            "item_count": len(normalized_items),
            "subtotal": round(subtotal, 2),
            "items": normalized_items,
            "raw_cart": cart_v2,
        }

    def cart_add(
        self,
        product_id: str,
        sku_id: Optional[str] = None,
        quantity: int = 1,
        confirm: bool = False,
    ) -> Dict[str, Any]:
        """
        Adds an item to the H-E-B shopping cart via 'cartItemV2'.
        Requires confirm=True to execute (Human-in-the-Loop Gate).
        Verifies addition via cart_get().
        """
        effective_sku_id = str(sku_id or product_id).strip()
        effective_product_id = str(product_id).strip()

        if not confirm:
            return {
                "preview": True,
                "action": "cart_add",
                "product_id": effective_product_id,
                "sku_id": effective_sku_id,
                "quantity": quantity,
                "message": "Preview: Set confirm=True to stage this item into your live H-E-B cart.",
            }

        state = self.get_session()
        variables = {
            "userIsLoggedIn": True,
            "productId": effective_product_id,
            "skuId": effective_sku_id,
            "quantity": int(quantity),
        }

        res = self.execute_persisted_query("cartItemV2", variables, session_state=state)

        # Verification pass: check cart state
        try:
            cart_now = self.cart_get()
            matched = any(
                str(i["product_id"]) == effective_product_id or str(i["sku_id"]) == effective_sku_id
                for i in cart_now.get("items", [])
            )
            return {
                "status": "SUCCESS",
                "action": "cart_add",
                "verified_in_cart": matched,
                "product_id": effective_product_id,
                "sku_id": effective_sku_id,
                "quantity": quantity,
                "cart_item_count": cart_now.get("item_count"),
                "cart_subtotal": cart_now.get("subtotal"),
                "raw_result": res,
            }
        except Exception:
            return {
                "status": "STAGED_UNVERIFIED",
                "product_id": effective_product_id,
                "sku_id": effective_sku_id,
                "quantity": quantity,
                "raw_result": res,
            }

    def cart_add_many(
        self,
        items: List[Dict[str, Any]],
        confirm: bool = False,
    ) -> Dict[str, Any]:
        """
        Adds multiple items to the cart in sequence.
        Requires confirm=True to execute.
        """
        if not confirm:
            return {
                "preview": True,
                "action": "cart_add_many",
                "count": len(items),
                "items": items,
                "message": f"Preview: Set confirm=True to stage {len(items)} items into your H-E-B cart.",
            }

        results = []
        for item in items:
            pid = str(item.get("product_id") or "")
            sid = str(item.get("sku_id") or pid)
            qty = int(item.get("quantity") or 1)
            res = self.cart_add(product_id=pid, sku_id=sid, quantity=qty, confirm=True)
            results.append(res)

        cart_now = self.cart_get()
        return {
            "status": "SUCCESS",
            "items_added": len(results),
            "cart_item_count": cart_now.get("item_count"),
            "cart_subtotal": cart_now.get("subtotal"),
            "details": results,
        }

    def cart_remove(
        self,
        product_id: str,
        sku_id: Optional[str] = None,
        confirm: bool = False,
    ) -> Dict[str, Any]:
        """
        Removes an item from the cart by sending cartItemV2 with quantity 0.
        """
        effective_sku_id = str(sku_id or product_id).strip()
        effective_product_id = str(product_id).strip()

        if not confirm:
            return {
                "preview": True,
                "action": "cart_remove",
                "product_id": effective_product_id,
                "sku_id": effective_sku_id,
                "message": "Preview: Set confirm=True to remove this item from your live H-E-B cart.",
            }

        state = self.get_session()
        variables = {
            "userIsLoggedIn": True,
            "productId": effective_product_id,
            "skuId": effective_sku_id,
            "quantity": 0,  # 0 quantity removes item
        }
        res = self.execute_persisted_query("cartItemV2", variables, session_state=state)

        cart_now = self.cart_get()
        return {
            "status": "SUCCESS",
            "action": "cart_remove",
            "product_id": effective_product_id,
            "cart_item_count": cart_now.get("item_count"),
            "cart_subtotal": cart_now.get("subtotal"),
            "raw_result": res,
        }

    def coupon_clip(self, coupon_id: int, confirm: bool = False) -> Dict[str, Any]:
        """
        Clips an H-E-B digital coupon to the user account using persisted query 'CouponClip'.
        """
        cid = int(coupon_id)
        if not confirm:
            return {
                "preview": True,
                "action": "coupon_clip",
                "coupon_id": cid,
                "message": f"Preview: Set confirm=True to clip digital coupon #{cid}.",
            }

        state = self.get_session()
        variables = {
            "userIsLoggedIn": True,
            "id": cid,
        }
        res = self.execute_persisted_query("CouponClip", variables, session_state=state)
        return {
            "status": "SUCCESS",
            "action": "coupon_clip",
            "coupon_id": cid,
            "raw_result": res,
        }

    def product_search(self, query: str, store_id: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
        """
        Searches H-E-B product catalog using authenticated SSR __NEXT_DATA__ extraction.
        Extracts real product IDs, SKUs, pricing, and stock status.
        """
        active_store = store_id or self.store_id
        state = self.get_session()

        url = f"https://www.heb.com/search?q={query.replace(' ', '+')}"
        headers = {
            "User-Agent": state.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }

        # Strategy 1: CDP Page fetch if CDP is connected
        if state.cdp_page:
            try:
                state.cdp_page.goto(url, wait_until="domcontentloaded", timeout=15000)
                content = state.cdp_page.content()
                if "errorCode" in content or "Pardon Our Interruption" in content or "reese84" in content.lower():
                    raise HEBWAFChallengeError(
                        f"Imperva Anti-Bot WAF challenge encountered while searching for '{query}' at store {active_store}."
                    )
                products = self._parse_next_data_html(content, limit)
                if products:
                    return products
            except HEBWAFChallengeError:
                raise
            except Exception as ex:
                logger.debug(f"CDP search navigation note: {ex}")

        # Strategy 2: Authenticated request with harvested cookies via curl_cffi
        try:
            from curl_cffi import requests as cffi_requests
            resp = cffi_requests.get(
                url,
                headers=headers,
                cookies=state.cookies,
                impersonate="chrome120",
                timeout=15,
            )
            if resp.status_code in (401, 403) or "errorCode" in resp.text or "Pardon Our Interruption" in resp.text:
                raise HEBWAFChallengeError(
                    f"Imperva Anti-Bot WAF challenge (HTTP {resp.status_code}) intercepted product search for '{query}'."
                )
            if resp.status_code == 200:
                products = self._parse_next_data_html(resp.text, limit)
                if products:
                    return products
        except HEBWAFChallengeError:
            raise
        except Exception as e:
            logger.debug(f"curl_cffi search note: {e}")

        return []

    def product_search_batch(self, queries: List[str], store_id: Optional[str] = None, limit: int = 10) -> Dict[str, List[Dict[str, Any]]]:
        """Searches multiple ingredient queries in batch."""
        results = {}
        for q in queries:
            results[q] = self.product_search(q, store_id=store_id, limit=limit)
        return results

    def _parse_next_data_html(self, html: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Parses __NEXT_DATA__ JSON from H-E-B search result HTML."""
        match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.DOTALL)
        if not match:
            return []

        try:
            data = json.loads(match.group(1))
            layout = data.get("props", {}).get("pageProps", {}).get("layout", {})
            visual_components = layout.get("visualComponents", [])
            search_grid = next((c for c in visual_components if c.get("type") == "searchGridV2"), None)
            if not search_grid:
                return []

            items = search_grid.get("items", [])
            products = []
            for item in items[:limit]:
                if item.get("__typename") != "Product":
                    continue

                product_id = str(item.get("id", "")).strip()
                if not product_id or not product_id.isdigit():
                    continue
                name = item.get("fullDisplayName") or item.get("displayName", "")
                brand = (item.get("brand") or {}).get("name") if isinstance(item.get("brand"), dict) else None

                skus = item.get("SKUs", [])
                sku_data = skus[0] if skus else {}
                sku_id = str(sku_data.get("id", product_id))
                size = sku_data.get("customerFriendlySize", "")

                price = 0.0
                for cp in sku_data.get("contextPrices", []):
                    if cp.get("context") in ("CURBSIDE", "CURBSIDE_PICKUP", "ONLINE"):
                        sp = cp.get("salePrice", {})
                        lp = cp.get("listPrice", {})
                        price = float(sp.get("amount") or lp.get("amount") or 0.0)
                        break

                inv = item.get("inventory", {})
                in_stock = inv.get("inventoryState") == "IN_STOCK"

                products.append({
                    "product_id": product_id,
                    "sku_id": sku_id,
                    "name": name,
                    "brand": brand or "H-E-B",
                    "package_size": size or "Standard",
                    "price": price,
                    "in_stock": in_stock,
                    "url": f"https://www.heb.com/product-detail/{product_id}",
                })
            return products
        except Exception as ex:
            logger.debug(f"Failed to parse __NEXT_DATA__: {ex}")
            return []


# ==============================================================================
# Module Helpers
# ==============================================================================

def cart_get(store_id: str = "639") -> Dict[str, Any]:
    """Retrieves current cart items via persisted query cartEstimated."""
    client = HEBGraphQLClient(store_id=store_id)
    return client.cart_get()


def cart_add(
    product_id: str,
    sku_id: Optional[str] = None,
    quantity: int = 1,
    confirm: bool = False,
    store_id: str = "639",
) -> Dict[str, Any]:
    """Adds product to cart via cartItemV2 mutation (confirm=True required)."""
    client = HEBGraphQLClient(store_id=store_id)
    return client.cart_add(product_id=product_id, sku_id=sku_id, quantity=quantity, confirm=confirm)


def cart_add_many(
    items: List[Dict[str, Any]],
    confirm: bool = False,
    store_id: str = "639",
) -> Dict[str, Any]:
    """Adds multiple items to cart via cartItemV2 mutation (confirm=True required)."""
    client = HEBGraphQLClient(store_id=store_id)
    return client.cart_add_many(items=items, confirm=confirm)


def cart_remove(
    product_id: str,
    sku_id: Optional[str] = None,
    confirm: bool = False,
    store_id: str = "639",
) -> Dict[str, Any]:
    """Removes product from cart via cartItemV2 with quantity 0 (confirm=True required)."""
    client = HEBGraphQLClient(store_id=store_id)
    return client.cart_remove(product_id=product_id, sku_id=sku_id, confirm=confirm)


def coupon_clip(
    coupon_id: int,
    confirm: bool = False,
    store_id: str = "639",
) -> Dict[str, Any]:
    """Clips digital coupon via CouponClip mutation (confirm=True required)."""
    client = HEBGraphQLClient(store_id=store_id)
    return client.coupon_clip(coupon_id=coupon_id, confirm=confirm)


def product_search(
    query: str,
    store_id: str = "639",
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Searches products via authenticated SSR __NEXT_DATA__."""
    client = HEBGraphQLClient(store_id=store_id)
    return client.product_search(query=query, store_id=store_id, limit=limit)


def product_search_batch(
    queries: List[str],
    store_id: str = "639",
    limit: int = 10,
) -> Dict[str, List[Dict[str, Any]]]:
    """Searches multiple products in batch."""
    client = HEBGraphQLClient(store_id=store_id)
    return client.product_search_batch(queries=queries, store_id=store_id, limit=limit)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Direct H-E-B GraphQL Engine (texas-grocery-mcp)")
    parser.add_argument("--search", default=None, help="Search query for products")
    parser.add_argument("--batch-search", default=None, help="Comma-separated product queries")
    parser.add_argument("--store-id", default=None, help="H-E-B store ID (defaults to HEB_STORE_ID or 639)")
    parser.add_argument("--cart-get", action="store_true", help="Retrieve current cart contents")
    parser.add_argument("--cart-add", default=None, help="Product ID to add to cart")
    parser.add_argument("--cart-remove", default=None, help="Product ID to remove from cart")
    parser.add_argument("--coupon-clip", type=int, default=None, help="Coupon ID to clip")
    parser.add_argument("--sku", default=None, help="SKU ID (optional)")
    parser.add_argument("--qty", type=int, default=1, help="Quantity (default 1)")
    parser.add_argument("--confirm", action="store_true", help="Confirm execution for cart and coupon mutations")

    args = parser.parse_args()
    store = args.store_id or os.environ.get("HEB_STORE_ID", "639")
    client = HEBGraphQLClient(store_id=store)

    if args.cart_get:
        print("=== H-E-B Cart Contents ===")
        res = client.cart_get()
        print(json.dumps(res, indent=2))
        sys.exit(0)

    elif args.cart_add:
        print(f"=== H-E-B Cart Add: {args.cart_add} (confirm={args.confirm}) ===")
        res = client.cart_add(
            product_id=args.cart_add,
            sku_id=args.sku,
            quantity=args.qty,
            confirm=args.confirm,
        )
        print(json.dumps(res, indent=2))
        sys.exit(0)

    elif args.cart_remove:
        print(f"=== H-E-B Cart Remove: {args.cart_remove} (confirm={args.confirm}) ===")
        res = client.cart_remove(
            product_id=args.cart_remove,
            sku_id=args.sku,
            confirm=args.confirm,
        )
        print(json.dumps(res, indent=2))
        sys.exit(0)

    elif args.coupon_clip is not None:
        print(f"=== H-E-B Coupon Clip: #{args.coupon_clip} (confirm={args.confirm}) ===")
        res = client.coupon_clip(
            coupon_id=args.coupon_clip,
            confirm=args.confirm,
        )
        print(json.dumps(res, indent=2))
        sys.exit(0)

    elif args.batch_search:
        queries = [q.strip() for q in args.batch_search.split(",") if q.strip()]
        print(f"=== H-E-B Batch Search ({len(queries)} queries) ===")
        res = client.product_search_batch(queries, store_id=store)
        print(json.dumps(res, indent=2))
        sys.exit(0)

    elif args.search:
        print(f"=== H-E-B Product Search: '{args.search}' ===")
        res = client.product_search(args.search, store_id=store)
        print(json.dumps(res, indent=2))
        sys.exit(0)

    else:
        parser.print_help()
        sys.exit(0)

