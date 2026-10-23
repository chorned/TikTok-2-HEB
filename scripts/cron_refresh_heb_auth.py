#!/usr/bin/env python3
"""
scripts/cron_refresh_heb_auth.py

Recurring runner to keep H-E-B session tokens (sat, sst, reese84) fresh and validated.
Logs results and sends notifications directly to Slack channel #hermes-log (C0B2Y77KFCG).
"""

import os
import sys
import json
import time
import datetime
import urllib.request
import urllib.error
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.cdp_harvester import check_sat_token
from scripts.heb_client import HEBClient
from scripts.heb_graphql import HEBAuthenticationError, HEBWAFChallengeError, HEBClientError

# Slack target (optional notification channel; defaults to #hermes-log if unset)
SLACK_CHANNEL = os.environ.get("SLACK_CHANNEL", "C0B2Y77KFCG")


def get_slack_token() -> str:
    """Retrieve Slack Bot Token from env or golem .env."""
    token = os.environ.get("SLACK_BOT_TOKEN")
    if token:
        return token
    for p in [Path("/Users/hermes/projects/golem/.env"), PROJECT_ROOT / ".env"]:
        if p.is_file():
            try:
                from dotenv import dotenv_values
                vals = dotenv_values(p)
                if vals.get("SLACK_BOT_TOKEN"):
                    return vals["SLACK_BOT_TOKEN"]
            except Exception:
                pass
    return ""


def post_to_slack(text: str, token: str, channel: str = SLACK_CHANNEL) -> bool:
    """Post message to Slack channel via Slack Web API."""
    if not token:
        print("[WARNING] No SLACK_BOT_TOKEN found; skipping Slack notification.")
        return False
    url = "https://slack.com/api/chat.postMessage"
    payload = {
        "channel": channel,
        "text": text,
        "unfurl_links": False,
        "unfurl_media": False,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            res_data = json.loads(resp.read().decode("utf-8"))
            if not res_data.get("ok"):
                print(f"[ERROR] Slack postMessage failed: {res_data.get('error')}")
                return False
            return True
    except Exception as ex:
        print(f"[ERROR] Exception posting to Slack: {ex}")
        return False


def run_refresh() -> int:
    """Execute H-E-B session validation and refresh."""
    start_time = time.time()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    slack_token = get_slack_token()

    print(f"[{now_str}] Starting H-E-B Auth Refresh check...")

    client = HEBClient(store_id="639")
    cart = None
    last_error = None

    # Step 1: Attempt validation pass with current session state
    try:
        cart = client.cart_get()
    except (HEBAuthenticationError, HEBWAFChallengeError, HEBClientError) as initial_err:
        last_error = initial_err
        print(f"[{now_str}] Initial cart_get encountered {type(initial_err).__name__}: {initial_err}. Attempting session refresh...")

        # Step 2: Session refresh pass
        try:
            client.harvest_session(ensure_warmup=True)
            cart = client.cart_get()
        except Exception as ref_err:
            last_error = ref_err
            print(f"[{now_str}] Secondary cart_get after refresh failed: {ref_err}")

    # Step 3: Handle outcome
    session = client.graphql_client.get_session()
    sat_valid = session.is_sat_valid
    sat_exp = session.sat_expires_at
    sat_exp_str = (
        datetime.datetime.fromtimestamp(sat_exp).strftime("%Y-%m-%d %H:%M:%S")
        if sat_exp
        else "Unknown"
    )

    if cart and cart.get("status") == "SUCCESS":
        raw_cart = cart.get("raw_cart", {})
        fulfillment = raw_cart.get("fulfillment", {}) or {}
        mode = fulfillment.get("curbsideFulfillmentMode", "unknown").capitalize()
        addr_obj = fulfillment.get("deliveryAddress") or {}
        address = addr_obj.get("address1") or "Mueller H-E-B (#639)"
        item_count = cart.get("item_count", 0)
        subtotal = cart.get("subtotal", 0.0)

        duration = time.time() - start_time
        success_msg = (
            f":white_check_mark: *H-E-B Auth Refresh — Succeeded*\n"
            f"• *Status:* Live & Validated via GraphQL (`cartEstimated`)\n"
            f"• *Source:* `{session.source}` | *reese84:* `{'Valid' if session.has_valid_reese84 else 'Present'}`\n"
            f"• *SAT Expiry:* `{sat_exp_str}`\n"
            f"• *Fulfillment Mode:* {mode} (`{address}`)\n"
            f"• *Cart State:* {item_count} items (${subtotal:.2f})\n"
            f"• *Latency:* {duration:.2f}s"
        )
        print(f"[SUCCESS]\n{success_msg}")
        post_to_slack(success_msg, slack_token)
        return 0

    # Failure branch
    err_type = type(last_error).__name__ if last_error else "UnknownError"
    err_msg = (
        f":warning: *H-E-B Auth Refresh — Attention Required*\n"
        f"• *Time:* `{now_str}`\n"
        f"• *Error Type:* `{err_type}`\n"
        f"• *Details:* {last_error}\n"
        f"• *Action:* Visit https://www.heb.com in Safari to re-authenticate."
    )
    print(f"[ERROR]\n{err_msg}")
    post_to_slack(err_msg, slack_token)
    return 1


if __name__ == "__main__":
    sys.exit(run_refresh())
