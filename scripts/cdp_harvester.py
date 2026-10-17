#!/usr/bin/env python3
"""
scripts/cdp_harvester.py

Lightweight Chrome CDP Session & Cookie Harvester targeting 127.0.0.1:9222.
Prioritizes live Chrome over CDP on 127.0.0.1:9222.
Supports fallback/support for local Chrome cookie extraction via rookiepy
or saved session state in cache/heb_session.json.

Zero slow UI clicks: purely network/context cookie harvesting and
lightweight page warm-up for bot tokens (reese84, sst, etc.).
"""

import os
import sys
import json
import time
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger("cdp_harvester")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SESSION_FILE = PROJECT_ROOT / "cache" / "heb_session.json"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)


@dataclass
class SessionState:
    """Represents the harvested H-E-B authentication and cookie state."""
    source: str  # "cdp", "rookiepy", or "cache"
    cookies: Dict[str, str] = field(default_factory=dict)
    raw_cookies: List[Dict[str, Any]] = field(default_factory=list)
    user_agent: str = DEFAULT_USER_AGENT
    is_authenticated: bool = False
    has_reese84: bool = False
    has_sst: bool = False
    cdp_page: Optional[Any] = None
    cdp_context: Optional[Any] = None
    cdp_browser: Optional[Any] = None

    def get_cookie_header(self) -> str:
        """Returns Cookie header formatted string."""
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())


class CDPSessionHarvester:
    """Lightweight Chrome CDP Session/Cookie Harvester targeting 127.0.0.1:9222."""

    def __init__(self, cdp_url: str = "http://127.0.0.1:9222", session_path: Optional[Path] = None):
        self.cdp_url = cdp_url
        self.session_path = session_path or SESSION_FILE
        self.session_path.parent.mkdir(parents=True, exist_ok=True)

    def is_cdp_available(self) -> bool:
        """Quickly checks if Chrome remote debugging port 9222 is listening."""
        try:
            import urllib.request
            req = urllib.request.Request(f"{self.cdp_url}/json/version", headers={"User-Agent": "HarvesterProbe/1.0"})
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                return resp.status == 200
        except Exception:
            return False

    def harvest(self, ensure_warmup: bool = True, launch_if_missing: bool = False) -> SessionState:
        """
        Harvests live cookies prioritizing Chrome over CDP (127.0.0.1:9222).
        Falls back to rookiepy local Chrome extraction or saved cache.
        """
        # 1. Attempt Chrome CDP on 127.0.0.1:9222
        cdp_state = self._harvest_from_cdp(ensure_warmup=ensure_warmup, launch_if_missing=launch_if_missing)
        if cdp_state:
            self._persist_cookies(cdp_state.raw_cookies)
            return cdp_state

        # 2. Fallback to local Chrome cookie extraction via rookiepy
        rookie_state = self._harvest_from_rookiepy()
        if rookie_state:
            logger.info(f"Harvested {len(rookie_state.cookies)} cookies via rookiepy from local Chrome.")
            self._persist_cookies(rookie_state.raw_cookies)
            return rookie_state

        # 3. Fallback to cached session file
        cache_state = self._harvest_from_cache()
        if cache_state:
            logger.info(f"Loaded {len(cache_state.cookies)} cookies from persistent cache {self.session_path}.")
            return cache_state

        logger.warning("No live CDP, rookiepy, or cached session cookies available.")
        return SessionState(source="none")

    def _harvest_from_cdp(self, ensure_warmup: bool = True, launch_if_missing: bool = False) -> Optional[SessionState]:
        """Harvests cookies directly from the active Chrome instance on port 9222."""
        if not self.is_cdp_available():
            if not launch_if_missing:
                logger.debug(f"CDP port {self.cdp_url} is not responding.")
                return None

        try:
            from playwright.sync_api import sync_playwright
            p = sync_playwright().start()
            browser = p.chromium.connect_over_cdp(self.cdp_url)
            context = browser.contexts[0] if browser.contexts else browser.new_context()

            # Check for existing heb.com page
            heb_pages = [pg for pg in context.pages if "heb.com" in pg.url]
            page = heb_pages[0] if heb_pages else None

            if page is None:
                page = context.new_page()

            if ensure_warmup:
                # If page is blank or not on heb.com, do a lightweight navigation to refresh tokens
                if "heb.com" not in page.url:
                    logger.info("Performing lightweight CDP warmup on https://www.heb.com to refresh bot tokens...")
                    page.goto("https://www.heb.com/", wait_until="domcontentloaded", timeout=20000)
                    page.wait_for_timeout(2000)

            # Extract user agent from the real Chrome instance
            try:
                user_agent = page.evaluate("navigator.userAgent")
            except Exception:
                user_agent = DEFAULT_USER_AGENT

            # Harvest all cookies from the context
            raw_cookies = context.cookies(["https://www.heb.com", "https://accounts.heb.com"])
            cookies_dict = {c["name"]: c["value"] for c in raw_cookies}

            has_reese84 = "reese84" in cookies_dict
            has_sst = "sst" in cookies_dict
            is_auth = any(k in cookies_dict for k in ["sst", "sat", "JSESSIONID", "loginEmail"])

            logger.info(
                f"Successfully harvested {len(cookies_dict)} cookies over CDP 9222 "
                f"(reese84={has_reese84}, sst={has_sst}, authenticated={is_auth})"
            )

            return SessionState(
                source="cdp",
                cookies=cookies_dict,
                raw_cookies=raw_cookies,
                user_agent=user_agent,
                is_authenticated=is_auth,
                has_reese84=has_reese84,
                has_sst=has_sst,
                cdp_page=page,
                cdp_context=context,
                cdp_browser=browser,
            )

        except Exception as ex:
            logger.debug(f"CDP harvesting note: {ex}")
            return None

    def _harvest_from_rookiepy(self) -> Optional[SessionState]:
        """Harvests cookies from local Google Chrome database using rookiepy."""
        try:
            import rookiepy
            domains = ["heb.com", ".heb.com", "accounts.heb.com"]
            rookie_cookies = rookiepy.chrome(domains)
            if not rookie_cookies:
                return None

            raw_cookies = []
            cookies_dict = {}
            for c in rookie_cookies:
                name = c.get("name")
                val = c.get("value")
                if name and val:
                    cookies_dict[name] = val
                    raw_cookies.append({
                        "name": name,
                        "value": val,
                        "domain": c.get("domain", ".heb.com"),
                        "path": c.get("path", "/"),
                        "secure": bool(c.get("secure", True)),
                        "httpOnly": bool(c.get("http_only", False)),
                    })

            # Check if any cached reese84 token exists to merge
            cached_cookies = self._read_cache_file()
            for cc in cached_cookies:
                cname = cc.get("name")
                if cname not in cookies_dict and cc.get("value"):
                    cookies_dict[cname] = cc["value"]
                    raw_cookies.append(cc)

            has_reese84 = "reese84" in cookies_dict
            has_sst = "sst" in cookies_dict
            is_auth = any(k in cookies_dict for k in ["sst", "sat", "JSESSIONID", "loginEmail"])

            return SessionState(
                source="rookiepy",
                cookies=cookies_dict,
                raw_cookies=raw_cookies,
                user_agent=DEFAULT_USER_AGENT,
                is_authenticated=is_auth,
                has_reese84=has_reese84,
                has_sst=has_sst,
            )
        except Exception as ex:
            logger.debug(f"Rookiepy extraction note: {ex}")
            return None

    def _harvest_from_cache(self) -> Optional[SessionState]:
        """Loads cookies from persistent session cache."""
        raw_cookies = self._read_cache_file()
        if not raw_cookies:
            return None

        cookies_dict = {c["name"]: c["value"] for c in raw_cookies if c.get("name") and c.get("value")}
        has_reese84 = "reese84" in cookies_dict
        has_sst = "sst" in cookies_dict
        is_auth = any(k in cookies_dict for k in ["sst", "sat", "JSESSIONID", "loginEmail"])

        return SessionState(
            source="cache",
            cookies=cookies_dict,
            raw_cookies=raw_cookies,
            user_agent=DEFAULT_USER_AGENT,
            is_authenticated=is_auth,
            has_reese84=has_reese84,
            has_sst=has_sst,
        )

    def _read_cache_file(self) -> List[Dict[str, Any]]:
        """Reads raw cookie list from session JSON file."""
        if self.session_path.is_file():
            try:
                with open(self.session_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        return data
            except Exception:
                pass
        return []

    def _persist_cookies(self, cookies: List[Dict[str, Any]]):
        """Persists cookies securely with 0600 permissions."""
        if not cookies:
            return
        try:
            with open(self.session_path, "w", encoding="utf-8") as f:
                json.dump(cookies, f, indent=2)
            os.chmod(self.session_path, 0o600)
            logger.debug(f"Persisted {len(cookies)} cookies to {self.session_path}")
        except Exception as ex:
            logger.warning(f"Could not persist session cookies: {ex}")


def get_live_session(cdp_url: str = "http://127.0.0.1:9222", ensure_warmup: bool = True) -> SessionState:
    """Helper function to obtain live H-E-B session state."""
    harvester = CDPSessionHarvester(cdp_url=cdp_url)
    return harvester.harvest(ensure_warmup=ensure_warmup)


if __name__ == "__main__":
    state = get_live_session()
    print("=== CDP / Cookie Harvester Result ===")
    print(f"Source: {state.source}")
    print(f"Total Cookies: {len(state.cookies)}")
    print(f"Authenticated (sst/sat): {state.is_authenticated}")
    print(f"reese84 Token Present: {state.has_reese84}")
    sample_names = list(state.cookies.keys())[:10]
    print(f"Sample Cookie Names: {sample_names}")
