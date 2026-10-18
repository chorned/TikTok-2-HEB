---
name: tiktok-2-heb
description: Converts short-form cooking and recipe videos (TikTok, Instagram Reels, YouTube Shorts) into scaled, pantry-audited H-E-B grocery carts ready for curbside pickup.
---

# TikTok-2-HEB Agent Skill

Transform incoming cooking and recipe video URLs into scaled, pantry-audited H-E-B grocery carts with kid-friendly culinary adaptations and deterministic out-of-stock substitutions.

---

## 🎯 Trigger Conditions

Activate this skill whenever:
1. An authorized user shares a cooking or recipe URL from **TikTok** (`tiktok.com`, `vm.tiktok.com`), **Instagram Reels** (`instagram.com/reel/`), or **YouTube Shorts** (`youtube.com/shorts/`).
2. A user asks to convert, scale, or order groceries for a recipe video (e.g. *"Order the ingredients for this recipe from H-E-B"*, *"Put this TikTok dinner into my cart"*).
3. A user replies to an existing grocery review thread with post-staging adjustments (e.g. *"add soy sauce"*, *"swap regular salmon for wild sockeye"*, *"make that swap permanent"*).

---

## 🔒 Operational Invariants & Safety Guardrails

1. **Hard Human-in-the-Loop Checkout Gate:**
   - The agent stages items into the cart and clips digital coupons.
   - The agent **MUST NEVER** submit payment, book delivery/pickup windows, or finalize order checkout.
   - Final review and checkout execution always remain in the hands of the user inside the official **My H-E-B** mobile app.
2. **Zero-Auth Extraction:**
   - Always invoke `scripts/extract_recipe_media.py` without requiring personal TikTok/Instagram logins or session cookies.
3. **Anti-Cache-Poisoning Invariant:**
   - Out-of-Stock substitutions (Tier 2/3/4) must remain strictly ephemeral for that specific cart order.
   - **NEVER** overwrite `primary_sku` in `cache/recipes/<slug>.yaml` unless the user explicitly commands: *"make that swap permanent"* or *"always use this brand"*.
4. **Kid-Friendly Preparation Split:**
   - When `kid_friendly_split` is enabled, decouple capsaicin/heat (`sriracha`, `chili flakes`, `cayenne`, `jalapeños`) from the base skillet. Keep the main vessel mild, plate the child's portion first, and season adult portions at the end.
   - Add reminders to cut proteins and choking hazards into 1/2-inch bite-sized cubes.

---

## 🛠️ Step-by-Step Execution Workflow

### Step 1: Pre-Flight Cache Lookup
Before performing media extraction or API calls, check if the video has already been parsed and cached:

```bash
python scripts/cache_manager.py lookup --query "<VIDEO_URL_OR_ID>"
```

- **HIT:** Load cached recipe YAML. Proceed directly to **Step 4 (Cart Staging)** using cached `primary_sku` definitions.
- **MISS:** Proceed to **Step 2 (Media Extraction)**.

### Step 2: Zero-Auth Multimodal Media Extraction
Run the 3-pass extraction pipeline:

```bash
python scripts/extract_recipe_media.py "<VIDEO_URL>"
```

1. **Pass 1:** Captures video title, description, uploader, and auto-subtitles.
2. **Pass 2:** Cleans subtitle cues, strips timestamps, and deduplicates rolling caption repeats.
3. **Pass 3:** If transcript is sparse (< 3 culinary measurement tokens), automatically extracts audio stream or keyframes.

### Step 3: Recipe Parsing, Household Scaling & Pantry Audit
1. **Household Scaling:** Scale ingredient quantities from original yield (e.g. 4 servings) to target servings (default `2.5` for 2 adults + 1 child) using `HouseholdScaler`.
2. **Pantry Deduplication:** Check ingredients against `config/pantry_staples.json` using `PantryComparator`. Move matching items (salt, pepper, cooking oil, soy sauce, garlic powder) to `pantry_staples_skipped`.
3. **Kid Prep Reminders:** Generate kid-friendly adaptations if spicy ingredients or whole proteins are detected.

### Step 4: H-E-B Catalog Matching & 5-Tier Fallback Cascade
For each ingredient in `ingredients_to_buy` (or ad-hoc user requests), match against the local H-E-B store catalog using `scripts/heb_client.py`:

- **Tier 0 (Preferred SKU Resolution & Package Size Scaling):** Matches against `cache/preferred_skus.json` (canonical keys and alias arrays) populated from past orders and manual pins. Automatically calculates integer package quantities (e.g. 48 oz recipe volume / 32 oz package -> 2 packages). If in stock, stages directly as Tier 0. If out of stock, smoothly cascades to Tier 1.
- **Tier 1 (Exact SKU Match):** Add existing recipe primary SKU if in stock.
- **Tier 2 (Brand Hierarchy):** If primary is OOS, substitute using `brand_hierarchy` (`H-E-B` → `H-E-B Organics` → `Central Market` → `Mi Tienda` → `Hill Country Fare` → `National Brand`).
- **Tier 3 (Organic ↔ Conventional Swap):** Swap organic for conventional (or vice-versa) if store has only one in stock.
- **Tier 4 (Package Size Adjustment / Human Escalation):** Adjust quantities or flag category wipeout as `⚠️ OUT OF STOCK (NEEDS HUMAN CALL)`.

### Step 5: Direct GraphQL Cart Staging & Coupon Clipping (Zero Slow UI Clicks)
Stage items and clip coupons directly via authenticated GraphQL operations (persisted queries `cartEstimated`, `cartItemV2`, `CouponClip` vendored from `texas-grocery-mcp`) using harvested Chrome CDP (`127.0.0.1:9222`) / `rookiepy` session tokens:

```bash
# 1. Harvest or refresh live session tokens
python scripts/heb_client.py --harvest

# 2. Sync preferred SKUs from past orders & Buy It Again
python scripts/cache_manager.py update-preferred-skus

# 3. Manually pin a favorite product as preferred SKU (protected from auto-sync)
python scripts/cache_manager.py pin-preferred-sku \
  --ingredient "whole milk" \
  --sku "4122012345" \
  --product "123456" \
  --name "H-E-B Organics Whole Milk" \
  --package-size "1 gal"

# 4. Resolve an ad-hoc ingredient or test package sizing
python scripts/heb_client.py --resolve-sku "48 oz beef broth"

# 5. Stage candidate items (requires human-in-the-loop --confirm)
python scripts/heb_client.py --stage-sku "48 oz beef broth" --confirm
# or directly by product ID:
python scripts/heb_client.py --cart-add <PRODUCT_ID> [--sku <SKU_ID>] [--qty <N>] --confirm

# 6. Inspect current live cart
python scripts/heb_client.py --cart-get

# 7. Remove item from cart if swapped
python scripts/heb_client.py --cart-remove <PRODUCT_ID> [--sku <SKU_ID>] --confirm

# 8. Clip digital coupons
python scripts/heb_client.py --coupon-clip <COUPON_ID> --confirm
```

> [!IMPORTANT]
> **Human-in-the-Loop Checkout Gate:** AGY is strictly prohibited from executing checkout, payment, or delivery/pickup window booking mutations. Cart staging and digital coupon clipping only; Carl always reviews and taps final checkout in the My H-E-B mobile app.

### Step 6: Recipe Cache Persistence
Persist the parsed recipe to ensure future runs hit the Hot Path:

```bash
python scripts/cache_manager.py save --file <RECIPE_DATA_YAML>
```

### Step 7: Mobile Review Summary (iMessage / Slack)
Format the review for clean reading on mobile screens (avoid Markdown tables, use clean emoji bullet formatting):

```text
🛒 H-E-B Cart Staged: Crispy Honey Garlic Salmon Bites
Scaled for 2 Adults + 1 Kid (~2.5 servings)

👶 Kid Prep Note:
• Hold the 1 tbsp sriracha out of the main skillet glaze. Plate the kid's portion first, then toss adult portions with sriracha.
• Cut kid's salmon into 1/2-inch bite-sized cubes.

✅ Added to H-E-B Cart (4 items):
• 1x H-E-B Fresh Atlantic Salmon Fillet (~1.25 lb) — $13.74
• 1x H-E-B Pure Clover Honey (12 oz) — $4.48
• 1x Fresh Green Onions (1 bunch) — $0.88
• 1x H-E-B Jasmine Rice (32 oz) — $2.74

🔄 Auto-Subbed (Out of Stock at Store):
• 2x H-E-B Broccoli Florets (10 oz bag) — $4.56
  (1 lb Organic Crowns were OOS; subbed 2x 10oz bags for this order only)

🎟️ Coupons Clipped:
• $1.00 off H-E-B Fresh Seafood Tray

🏠 Pantry Check (Skipped — reply to add):
• Soy sauce (3 tbsp), Garlic (4 cloves), Cornstarch (2 tbsp), Sesame oil (1 tsp)

📱 Next Step:
Reply if you want to add a pantry item (e.g. "add soy sauce") or swap a brand. Otherwise, open the My H-E-B app to review and tap checkout!
```

---

## 🔄 Post-Staging Reply Commands

When the user replies to the staged review thread:
1. **"Add [pantry item]"** (e.g. *"we are out of soy sauce, add it"*):
   - Resolve through Tier-0 preferred SKU engine and stage in cart:
     `python scripts/heb_client.py --stage-sku "soy sauce" --confirm`
   - Confirm via brief reply: *"Added 1x H-E-B Reduced Sodium Soy Sauce to your cart."*
2. **"Swap [item] for [preference]"** (e.g. *"swap regular salmon for wild sockeye"*):
   - Remove old SKU from cart and add preferred SKU.
   - Prompt: *"Swapped for wild sockeye for this order. Would you like me to make this swap permanent for future orders of this recipe?"*
3. **"Make that swap permanent"**:
   - Execute `python scripts/cache_manager.py promote-sku ...` to update `primary_sku` in `cache/recipes/<slug>.yaml` and pin it in `cache/preferred_skus.json` with `source: "manual_override"`.
   - Confirm: *"Updated recipe cache: Central Market Wild Sockeye is now your primary SKU for this recipe and pinned as your preferred SKU."*
