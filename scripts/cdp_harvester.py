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
import struct
import logging
from dataclasses import dataclass, field
import base64
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger("cdp_harvester")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

MAC_EPOCH = 978307200.0  # Unix timestamp for Jan 1 2001 00:00:00 UTC (macOS Cocoa epoch)


def parse_safari_binarycookies(
    filepath: Optional[Path] = None,
    domains: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """
    Parses macOS Safari binarycookies file (Cookies.binarycookies) directly in Python.
    Bypasses rookiepy/rookie-rs C-string null-terminator parsing panics on macOS.
    Returns normalized cookie dictionaries compatible with Playwright and CookieJar.
    """
    paths = []
    if filepath:
        paths.append(Path(filepath))
    else:
        paths.extend([
            Path.home() / "Library/Containers/com.apple.Safari/Data/Library/Cookies/Cookies.binarycookies",
            Path.home() / "Library/Cookies/Cookies.binarycookies",
        ])

    target = next((p for p in paths if p.is_file()), None)
    if not target:
        return []

    try:
        data = target.read_bytes()
    except Exception as ex:
        logger.debug(f"Could not read Safari binarycookies at {target}: {ex}")
        return []

    if len(data) < 8 or data[:4] != b"cook":
        return []

    try:
        num_pages = struct.unpack(">I", data[4:8])[0]
        if len(data) < 8 + 4 * num_pages:
            return []
        page_sizes = [struct.unpack(">I", data[8 + 4 * i : 12 + 4 * i])[0] for i in range(num_pages)]
        pos = 8 + 4 * num_pages
        cookies: List[Dict[str, Any]] = []

        for size in page_sizes:
            page_data = data[pos : pos + size]
            pos += size
            if len(page_data) < 8:
                continue
            page_magic = struct.unpack(">I", page_data[:4])[0]
            num_cookies = struct.unpack("<I", page_data[4:8])[0]
            if page_magic != 0x00000100 or len(page_data) < 8 + 4 * num_cookies:
                continue
            cookie_offsets = [struct.unpack("<I", page_data[8 + 4 * i : 12 + 4 * i])[0] for i in range(num_cookies)]

            for c_off in cookie_offsets:
                if c_off >= len(page_data):
                    continue
                c_data = page_data[c_off:]
                if len(c_data) < 48:
                    continue
                c_size, _, flags, _ = struct.unpack("<IIII", c_data[:16])
                url_off, name_off, path_off, val_off = struct.unpack("<IIII", c_data[16:32])

                def read_str(offset: int) -> str:
                    if offset >= len(c_data):
                        return ""
                    end = c_data.find(b"\x00", offset)
                    if end == -1:
                        return c_data[offset:].decode("utf-8", errors="replace")
                    return c_data[offset:end].decode("utf-8", errors="replace")

                domain = read_str(url_off)
                name = read_str(name_off)
                path = read_str(path_off) or "/"
                val = read_str(val_off)

                if domains and not any(d in domain for d in domains):
                    continue

                expiry_mac = struct.unpack("<d", c_data[40:48])[0]
                expiry_unix = expiry_mac + MAC_EPOCH

                cookies.append({
                    "name": name,
                    "value": val,
                    "domain": domain,
                    "path": path,
                    "secure": bool(flags & 1),
                    "httpOnly": bool(flags & 4),
                    "expires": expiry_unix,
                })
        return cookies
    except Exception as ex:
        logger.debug(f"Safari binarycookies parsing note: {ex}")
        return []


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SESSION_FILE = PROJECT_ROOT / "cache" / "heb_session.json"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)


def is_reese84_valid(storage_state_or_data: Any) -> Tuple[bool, Optional[float]]:
    """
    Check if reese84 bot detection token is present and not expired.
    Vendored logic matching texas-grocery-mcp:
    Extracts reese84 from localStorage / dict and evaluates renewTime (ms)
    or renewInSec + serverTimestamp vs current time.
    """
    reese_data = None
    if isinstance(storage_state_or_data, dict):
        origins = storage_state_or_data.get("origins", [])
        if origins:
            local_storage = origins[0].get("localStorage", [])
            for item in local_storage:
                if item.get("name") == "reese84":
                    val = item.get("value", "{}")
                    try:
                        reese_data = json.loads(val) if isinstance(val, str) else val
                    except Exception:
                        pass
                    break
        elif "renewTime" in storage_state_or_data or "renewInSec" in storage_state_or_data:
            reese_data = storage_state_or_data
        elif "reese84" in storage_state_or_data:
            val = storage_state_or_data["reese84"]
            try:
                reese_data = json.loads(val) if isinstance(val, str) else val
            except Exception:
                pass
    elif isinstance(storage_state_or_data, str):
        try:
            reese_data = json.loads(storage_state_or_data)
        except Exception:
            pass

    if not reese_data or not isinstance(reese_data, dict):
        return False, None

    now = time.time()
    renew_time_ms = reese_data.get("renewTime")
    if renew_time_ms:
        try:
            expires_at = float(renew_time_ms) / 1000.0
            return now < expires_at, expires_at
        except (ValueError, TypeError):
            return False, None

    renew_in_sec = reese_data.get("renewInSec")
    server_timestamp = reese_data.get("serverTimestamp")
    if renew_in_sec and server_timestamp:
        try:
            server_ts = float(server_timestamp) / 1000.0 if float(server_timestamp) > 1e12 else float(server_timestamp)
            expires_at = server_ts + float(renew_in_sec)
            return now < expires_at, expires_at
        except (ValueError, TypeError):
            return False, None

    return True, None


def check_sat_token(sat_value: str) -> Tuple[bool, Optional[float]]:
    """
    Inspects H-E-B sat (Session Access Token) JWT payload exp claim.
    Returns (is_valid, exp_timestamp).
    """
    if not sat_value or not isinstance(sat_value, str):
        return False, None
    try:
        parts = sat_value.split(".")
        if len(parts) >= 2:
            payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
            payload = json.loads(base64.b64decode(payload_b64).decode("utf-8"))
            exp = payload.get("exp")
            if exp is not None:
                exp_f = float(exp)
                return time.time() < exp_f, exp_f
    except Exception:
        pass
    return False, None


@dataclass
class SessionState:
    """Represents the harvested H-E-B authentication and cookie state."""
    source: str  # "cdp", "rookiepy", "cache", or "playwright_refresh"
    cookies: Dict[str, str] = field(default_factory=dict)
    raw_cookies: List[Dict[str, Any]] = field(default_factory=list)
    local_storage: Dict[str, Any] = field(default_factory=dict)
    storage_state: Dict[str, Any] = field(default_factory=dict)
    user_agent: str = DEFAULT_USER_AGENT
    is_authenticated: bool = False
    has_reese84: bool = False
    has_valid_reese84: bool = False
    has_sst: bool = False
    has_sat: bool = False
    is_sat_valid: bool = False
    sat_expires_at: Optional[float] = None
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

    def harvest(self, ensure_warmup: bool = True, launch_if_missing: bool = False, refresh_if_needed: bool = False) -> SessionState:
        """
        Harvests live cookies prioritizing Chrome over CDP (127.0.0.1:9222).
        Falls back to rookiepy local Chrome extraction or saved cache.
        If refresh_if_needed is True and reese84 token is missing/expired, executes 5-second Playwright refresh.
        """
        # 1. Attempt Chrome CDP on 127.0.0.1:9222
        cdp_state = self._harvest_from_cdp(ensure_warmup=ensure_warmup, launch_if_missing=launch_if_missing)
        if cdp_state:
            self._persist_cookies(cdp_state.raw_cookies)
            return cdp_state

        # 2. Fallback to local Chrome cookie extraction via rookiepy
        rookie_state = self._harvest_from_rookiepy()
        if rookie_state:
            logger.info(f"Harvested {len(rookie_state.cookies)} cookies via {rookie_state.source} from local browser.")
            self._persist_cookies(rookie_state.raw_cookies)
            if refresh_if_needed and not rookie_state.has_valid_reese84:
                refreshed = self.refresh_session_with_playwright(headless=True, timeout=20000, warmup_seconds=5.0)
                if refreshed and refreshed.source != "playwright_failed":
                    return refreshed
            return rookie_state

        # 3. Fallback to cached session file
        cache_state = self._harvest_from_cache()
        if cache_state:
            logger.info(f"Loaded {len(cache_state.cookies)} cookies from persistent cache {self.session_path}.")
            if refresh_if_needed and not cache_state.has_valid_reese84:
                refreshed = self.refresh_session_with_playwright(headless=True, timeout=20000, warmup_seconds=5.0)
                if refreshed and refreshed.source != "playwright_failed":
                    return refreshed
            return cache_state

        # 4. Direct Playwright warm-up attempt if no prior sessions existed
        if refresh_if_needed:
            refreshed = self.refresh_session_with_playwright(headless=True, timeout=20000, warmup_seconds=5.0)
            if refreshed and refreshed.source != "playwright_failed":
                return refreshed

        logger.warning("No live CDP, rookiepy, or cached session cookies available.")
        return SessionState(source="none")

    def refresh_session_with_playwright(
        self,
        headless: bool = True,
        timeout: int = 30000,
        warmup_seconds: float = 5.0,
    ) -> SessionState:
        """
        Refreshes H-E-B session and generates a fresh reese84 token via a 5-second
        Playwright navigation to https://www.heb.com, vendored from texas-grocery-mcp.
        Saves full storage_state (cookies + localStorage) to session_path.
        """
        logger.info(
            f"Initiating {warmup_seconds}s Playwright session & bot token refresh on https://www.heb.com..."
        )
        existing_cookies = self._read_cache_file()
        pw_cookies = []
        for c in existing_cookies:
            name = c.get("name")
            val = c.get("value")
            if not name or not val:
                continue
            # Skip stale reese84 to allow fresh minting
            if name == "reese84":
                continue
            pw_c = {
                "name": name,
                "value": val,
                "domain": c.get("domain", ".heb.com"),
                "path": c.get("path", "/"),
                "secure": bool(c.get("secure", True)),
                "httpOnly": bool(c.get("httpOnly", False)),
            }
            if c.get("sameSite") in ["Strict", "Lax", "None"]:
                pw_c["sameSite"] = c["sameSite"]
            pw_cookies.append(pw_c)

        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=headless,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-first-run",
                        "--no-default-browser-check",
                        "--disable-infobars",
                    ],
                )
                context = browser.new_context(
                    user_agent=DEFAULT_USER_AGENT,
                    viewport={"width": 1280, "height": 800},
                )
                if pw_cookies:
                    context.add_cookies(pw_cookies)

                page = context.new_page()
                resp = page.goto("https://www.heb.com", wait_until="domcontentloaded", timeout=timeout)
                status = resp.status if resp else 0
                logger.info(
                    f"Navigated to https://www.heb.com (HTTP {status}). Waiting {warmup_seconds}s for Imperva challenge & reese84 token..."
                )

                page.wait_for_timeout(int(warmup_seconds * 1000))

                # Save full storage state
                full_storage_state = context.storage_state()
                self._persist_storage_state(full_storage_state)

                # Harvest fresh cookies & localStorage from context
                raw_cookies = context.cookies()
                cookies_dict = {c["name"]: c["value"] for c in raw_cookies if c.get("name") and c.get("value")}

                local_storage_dict = {}
                for origin_obj in full_storage_state.get("origins", []):
                    if "heb.com" in origin_obj.get("origin", ""):
                        for item in origin_obj.get("localStorage", []):
                            local_storage_dict[item.get("name")] = item.get("value")

                reese_valid, _ = is_reese84_valid(full_storage_state)
                has_reese = "reese84" in cookies_dict or "reese84" in local_storage_dict
                has_sst = "sst" in cookies_dict
                has_sat = "sat" in cookies_dict
                is_sat_valid_flag, sat_exp = check_sat_token(cookies_dict.get("sat", "")) if has_sat else (False, None)
                is_auth = (has_sat and is_sat_valid_flag) or has_sst

                browser.close()

                logger.info(
                    f"Playwright refresh completed: {len(cookies_dict)} cookies, "
                    f"reese84={has_reese} (valid={reese_valid}), sat={has_sat} (valid={is_sat_valid_flag}, exp={sat_exp})"
                )

                return SessionState(
                    source="playwright_refresh",
                    cookies=cookies_dict,
                    raw_cookies=raw_cookies,
                    local_storage=local_storage_dict,
                    storage_state=full_storage_state,
                    user_agent=DEFAULT_USER_AGENT,
                    is_authenticated=is_auth,
                    has_reese84=has_reese,
                    has_valid_reese84=reese_valid,
                    has_sst=has_sst,
                    has_sat=has_sat,
                    is_sat_valid=is_sat_valid_flag,
                    sat_expires_at=sat_exp,
                )
        except Exception as ex:
            logger.warning(f"Playwright session refresh encountered error: {ex}")
            return SessionState(source="playwright_failed")

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
                if "heb.com" not in page.url:
                    logger.info("Performing lightweight CDP warmup on https://www.heb.com to refresh bot tokens...")
                    page.goto("https://www.heb.com/", wait_until="domcontentloaded", timeout=20000)
                    page.wait_for_timeout(2000)

            try:
                user_agent = page.evaluate("navigator.userAgent")
            except Exception:
                user_agent = DEFAULT_USER_AGENT

            raw_cookies = context.cookies(["https://www.heb.com", "https://accounts.heb.com"])
            cookies_dict = {c["name"]: c["value"] for c in raw_cookies}

            full_storage_state = context.storage_state()
            local_storage_dict = {}
            for origin_obj in full_storage_state.get("origins", []):
                if "heb.com" in origin_obj.get("origin", ""):
                    for item in origin_obj.get("localStorage", []):
                        local_storage_dict[item.get("name")] = item.get("value")

            reese_valid, _ = is_reese84_valid(full_storage_state)
            has_reese84 = "reese84" in cookies_dict or "reese84" in local_storage_dict
            has_sst = "sst" in cookies_dict
            has_sat = "sat" in cookies_dict
            is_sat_valid_flag, sat_exp = check_sat_token(cookies_dict.get("sat", "")) if has_sat else (False, None)
            is_auth = (has_sat and is_sat_valid_flag) or has_sst

            logger.info(
                f"Successfully harvested {len(cookies_dict)} cookies over CDP 9222 "
                f"(reese84={has_reese84}, sst={has_sst}, sat_valid={is_sat_valid_flag})"
            )

            return SessionState(
                source="cdp",
                cookies=cookies_dict,
                raw_cookies=raw_cookies,
                local_storage=local_storage_dict,
                storage_state=full_storage_state,
                user_agent=user_agent,
                is_authenticated=is_auth,
                has_reese84=has_reese84,
                has_valid_reese84=reese_valid,
                has_sst=has_sst,
                has_sat=has_sat,
                is_sat_valid=is_sat_valid_flag,
                sat_expires_at=sat_exp,
                cdp_page=page,
                cdp_context=context,
                cdp_browser=browser,
            )

        except Exception as ex:
            logger.debug(f"CDP harvesting note: {ex}")
            return None

    def _harvest_safari_cookies(self, domains: List[str]) -> List[Dict[str, Any]]:
        """Extracts cookies from Safari binarycookies, falling back to rookiepy.safari."""
        cookies = parse_safari_binarycookies(domains=domains)
        if cookies:
            return cookies
        try:
            import rookiepy
            raw = rookiepy.safari(domains)
            res = []
            for c in (raw or []):
                name = c.get("name")
                val = c.get("value")
                if name and val:
                    res.append({
                        "name": name,
                        "value": val,
                        "domain": c.get("domain", ".heb.com"),
                        "path": c.get("path", "/"),
                        "secure": bool(c.get("secure", True)),
                        "httpOnly": bool(c.get("http_only", False)),
                        "expires": c.get("expires"),
                    })
            return res
        except Exception:
            return []

    def _harvest_chrome_cookies(self, domains: List[str]) -> List[Dict[str, Any]]:
        """Extracts cookies from local Google Chrome database using rookiepy."""
        try:
            import rookiepy
            raw = rookiepy.chrome(domains)
            res = []
            for c in (raw or []):
                name = c.get("name")
                val = c.get("value")
                if name and val:
                    res.append({
                        "name": name,
                        "value": val,
                        "domain": c.get("domain", ".heb.com"),
                        "path": c.get("path", "/"),
                        "secure": bool(c.get("secure", True)),
                        "httpOnly": bool(c.get("http_only", False)),
                        "expires": c.get("expires"),
                    })
            return res
        except Exception:
            return []

    def _harvest_from_rookiepy(self) -> Optional[SessionState]:
        """Harvests cookies from Safari and Chrome, prioritizing valid authenticated sessions."""
        try:
            domains = ["heb.com", ".heb.com", "accounts.heb.com", "www.heb.com"]
            safari_cookies = self._harvest_safari_cookies(domains)
            chrome_cookies = self._harvest_chrome_cookies(domains)

            if not safari_cookies and not chrome_cookies:
                return None

            def evaluate_cookies(cookie_list: List[Dict[str, Any]]) -> Tuple[bool, Optional[float], bool, int]:
                c_dict = {c["name"]: c["value"] for c in cookie_list if c.get("name") and c.get("value")}
                has_sat = "sat" in c_dict
                sat_valid, sat_exp = check_sat_token(c_dict.get("sat", "")) if has_sat else (False, None)
                has_sst = "sst" in c_dict
                return sat_valid, sat_exp, has_sst, len(c_dict)

            safari_sat_valid, safari_sat_exp, safari_has_sst, safari_count = evaluate_cookies(safari_cookies)
            chrome_sat_valid, chrome_sat_exp, chrome_has_sst, chrome_count = evaluate_cookies(chrome_cookies)

            if safari_sat_valid and not chrome_sat_valid:
                chosen_source = "safari"
                primary_cookies = safari_cookies
                secondary_cookies = chrome_cookies
            elif chrome_sat_valid and not safari_sat_valid:
                chosen_source = "chrome"
                primary_cookies = chrome_cookies
                secondary_cookies = safari_cookies
            elif safari_cookies and not chrome_cookies:
                chosen_source = "safari"
                primary_cookies = safari_cookies
                secondary_cookies = []
            elif chrome_cookies and not safari_cookies:
                chosen_source = "chrome"
                primary_cookies = chrome_cookies
                secondary_cookies = []
            else:
                # Both present: prioritize Safari if it has recent sst or cookies
                if safari_has_sst or safari_count >= chrome_count:
                    chosen_source = "safari"
                    primary_cookies = safari_cookies
                    secondary_cookies = chrome_cookies
                else:
                    chosen_source = "chrome"
                    primary_cookies = chrome_cookies
                    secondary_cookies = safari_cookies

            raw_cookies: List[Dict[str, Any]] = []
            cookies_dict: Dict[str, str] = {}
            for c in primary_cookies:
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
                        "httpOnly": bool(c.get("httpOnly") or c.get("http_only", False)),
                    })

            for c in secondary_cookies:
                name = c.get("name")
                val = c.get("value")
                if name and val and name not in cookies_dict:
                    cookies_dict[name] = val
                    raw_cookies.append({
                        "name": name,
                        "value": val,
                        "domain": c.get("domain", ".heb.com"),
                        "path": c.get("path", "/"),
                        "secure": bool(c.get("secure", True)),
                        "httpOnly": bool(c.get("httpOnly") or c.get("http_only", False)),
                    })

            # Check if any cached session tokens (like sat/reese84) exist to merge
            cached_cookies = self._read_cache_file()
            for cc in cached_cookies:
                cname = cc.get("name")
                if cname not in cookies_dict and cc.get("value"):
                    cookies_dict[cname] = cc["value"]
                    raw_cookies.append(cc)

            cached_storage = self._read_cache_storage_state()
            local_storage_dict = {}
            for origin_obj in cached_storage.get("origins", []):
                if "heb.com" in origin_obj.get("origin", ""):
                    for item in origin_obj.get("localStorage", []):
                        local_storage_dict[item.get("name")] = item.get("value")

            reese_valid, _ = is_reese84_valid(cached_storage) if cached_storage else (False, None)
            has_reese84 = "reese84" in cookies_dict or "reese84" in local_storage_dict
            has_sst = "sst" in cookies_dict
            has_sat = "sat" in cookies_dict
            is_sat_valid_flag, sat_exp = check_sat_token(cookies_dict.get("sat", "")) if has_sat else (False, None)
            is_auth = (has_sat and is_sat_valid_flag) or has_sst

            return SessionState(
                source=chosen_source,
                cookies=cookies_dict,
                raw_cookies=raw_cookies,
                local_storage=local_storage_dict,
                storage_state=cached_storage or {"cookies": raw_cookies, "origins": []},
                user_agent=DEFAULT_USER_AGENT,
                is_authenticated=is_auth,
                has_reese84=has_reese84,
                has_valid_reese84=reese_valid,
                has_sst=has_sst,
                has_sat=has_sat,
                is_sat_valid=is_sat_valid_flag,
                sat_expires_at=sat_exp,
            )
        except Exception as ex:
            logger.debug(f"Local browser cookie extraction note: {ex}")
            return None

    def _harvest_from_cache(self) -> Optional[SessionState]:
        """Loads cookies and localStorage from persistent session cache."""
        if not self.session_path.is_file():
            return None
        try:
            with open(self.session_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return None

        if isinstance(data, dict):
            raw_cookies = data.get("cookies", [])
            storage_state = data
            local_storage_dict = {}
            for origin_obj in data.get("origins", []):
                if "heb.com" in origin_obj.get("origin", ""):
                    for item in origin_obj.get("localStorage", []):
                        local_storage_dict[item.get("name")] = item.get("value")
        elif isinstance(data, list):
            raw_cookies = data
            storage_state = {"cookies": data, "origins": []}
            local_storage_dict = {}
        else:
            return None

        if not raw_cookies:
            return None

        cookies_dict = {c["name"]: c["value"] for c in raw_cookies if c.get("name") and c.get("value")}
        reese_valid, _ = is_reese84_valid(storage_state)
        has_reese84 = "reese84" in cookies_dict or "reese84" in local_storage_dict
        has_sst = "sst" in cookies_dict
        has_sat = "sat" in cookies_dict
        is_sat_valid_flag, sat_exp = check_sat_token(cookies_dict.get("sat", "")) if has_sat else (False, None)
        is_auth = (has_sat and is_sat_valid_flag) or has_sst

        return SessionState(
            source="cache",
            cookies=cookies_dict,
            raw_cookies=raw_cookies,
            local_storage=local_storage_dict,
            storage_state=storage_state,
            user_agent=DEFAULT_USER_AGENT,
            is_authenticated=is_auth,
            has_reese84=has_reese84,
            has_valid_reese84=reese_valid,
            has_sst=has_sst,
            has_sat=has_sat,
            is_sat_valid=is_sat_valid_flag,
            sat_expires_at=sat_exp,
        )

    def _read_cache_storage_state(self) -> Dict[str, Any]:
        """Reads raw storage_state dictionary from session JSON file."""
        if self.session_path.is_file():
            try:
                with open(self.session_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict) and "cookies" in data:
                        return data
                    elif isinstance(data, list):
                        return {"cookies": data, "origins": []}
            except Exception:
                pass
        return {}

    def _read_cache_file(self) -> List[Dict[str, Any]]:
        """Reads raw cookie list from session JSON file (supports list and storage_state dict)."""
        if self.session_path.is_file():
            try:
                with open(self.session_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        return data
                    elif isinstance(data, dict):
                        return data.get("cookies", [])
            except Exception:
                pass
        return []

    def _persist_storage_state(self, storage_state: Dict[str, Any]):
        """Persists full Playwright storage_state (cookies + origins) with 0600 permissions."""
        if not storage_state:
            return
        try:
            with open(self.session_path, "w", encoding="utf-8") as f:
                json.dump(storage_state, f, indent=2)
            os.chmod(self.session_path, 0o600)
            logger.debug(f"Persisted storage state to {self.session_path}")
        except Exception as ex:
            logger.warning(f"Could not persist storage state: {ex}")

    def _persist_cookies(self, cookies: List[Dict[str, Any]]):
        """Persists cookies securely with 0600 permissions, preserving origins/localStorage if present."""
        if not cookies:
            return
        try:
            existing = {}
            if self.session_path.is_file():
                try:
                    with open(self.session_path, "r", encoding="utf-8") as f:
                        existing = json.load(f)
                except Exception:
                    existing = {}

            if isinstance(existing, dict) and "origins" in existing:
                existing["cookies"] = cookies
                data_to_write = existing
            else:
                data_to_write = cookies

            with open(self.session_path, "w", encoding="utf-8") as f:
                json.dump(data_to_write, f, indent=2)
            os.chmod(self.session_path, 0o600)
            logger.debug(f"Persisted {len(cookies)} cookies to {self.session_path}")
        except Exception as ex:
            logger.warning(f"Could not persist session cookies: {ex}")


def get_live_session(cdp_url: str = "http://127.0.0.1:9222", ensure_warmup: bool = True, refresh_if_needed: bool = True) -> SessionState:
    """Helper function to obtain live H-E-B session state."""
    harvester = CDPSessionHarvester(cdp_url=cdp_url)
    return harvester.harvest(ensure_warmup=ensure_warmup, refresh_if_needed=refresh_if_needed)


if __name__ == "__main__":
    state = get_live_session(refresh_if_needed=False)
    print("=== CDP / Cookie Harvester Result ===")
    print(f"Source: {state.source}")
    print(f"Total Cookies: {len(state.cookies)}")
    print(f"Authenticated: {state.is_authenticated} (sat_valid={state.is_sat_valid}, sat_exp={state.sat_expires_at})")
    print(f"reese84 Token Present: {state.has_reese84} (valid={state.has_valid_reese84})")
    sample_names = list(state.cookies.keys())[:10]
    print(f"Sample Cookie Names: {sample_names}")
