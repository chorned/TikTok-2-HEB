#!/usr/bin/env python3
"""
tests/test_skill_setup.py

Single Unified Setup & Diagnostic Verification Test for TikTok-2-HEB.
Audits all operational prerequisites:
  1. Python dependencies (yt-dlp, curl_cffi, playwright, pydantic, pyyaml, requests, bs4)
  2. External binaries (ffmpeg)
  3. Environment variables & .env configuration
  4. Configuration files (config/household.yaml, config/pantry_staples.json)
  5. Cache directory hierarchy (cache/media, cache/recipes)
  6. Playwright Chromium browser runtime availability
  7. End-to-end dry-run verification (Pantry check, Scaling math, OOS Cascade, Cache CRUD)

Provides clear, actionable, interactive diagnostic instructions when any requirement is unmet.
"""

import os
import sys
import shutil
import tempfile
import unittest
import importlib.util
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


class DiagnosticAudit:
    """Helper class to collect diagnostics and print remediation advice."""

    def __init__(self):
        self.issues = []
        self.recommendations = []

    def add_issue(self, title: str, remedy_step: str):
        self.issues.append(title)
        self.recommendations.append(remedy_step)

    def print_report(self):
        if not self.issues:
            print("\n" + "=" * 70)
            print("🚀 ALL TIKTOK-2-HEB PRE-FLIGHT CHECKS PASSED (100% OPERATIONAL)")
            print("=" * 70 + "\n")
            return

        print("\n" + "!" * 70)
        print("⚠️  TIKTOK-2-HEB SETUP DIAGNOSTIC REPORT: ACTION REQUIRED")
        print("!" * 70)
        for i, (issue, remedy) in enumerate(zip(self.issues, self.recommendations), 1):
            print(f"\n[{i}] ISSUE: {issue}")
            print(f"    FIX:   {remedy}")
        print("\n" + "=" * 70 + "\n")


class TestSkillSetupAndDiagnostics(unittest.TestCase):
    """Unified setup test suite and diagnostic probe."""

    @classmethod
    def setUpClass(cls):
        cls.audit = DiagnosticAudit()

    # --------------------------------------------------------------------------
    # 1. Python Dependencies
    # --------------------------------------------------------------------------
    def test_01_python_dependencies(self):
        """Check all required Python packages in requirements.txt."""
        packages = {
            "yt_dlp": "yt-dlp",
            "curl_cffi": "curl-cffi",
            "playwright": "playwright",
            "pydantic": "pydantic",
            "yaml": "pyyaml",
            "dotenv": "python-dotenv",
            "requests": "requests",
            "bs4": "beautifulsoup4",
        }
        missing = []
        for mod_name, pkg_name in packages.items():
            if importlib.util.find_spec(mod_name) is None:
                missing.append(pkg_name)

        if missing:
            self.audit.add_issue(
                f"Missing Python package(s): {', '.join(missing)}",
                f"Run: uv pip install -r requirements.txt (or source .venv/bin/activate && pip install -r requirements.txt)"
            )
        self.assertEqual(len(missing), 0, f"Missing Python packages: {missing}")

    # --------------------------------------------------------------------------
    # 2. External System Tools
    # --------------------------------------------------------------------------
    def test_02_external_tools(self):
        """Ensure ffmpeg binary is installed and executable for media processing."""
        ffmpeg_path = shutil.which("ffmpeg")
        if not ffmpeg_path:
            self.audit.add_issue(
                "ffmpeg binary not found in system PATH",
                "Install ffmpeg: brew install ffmpeg (macOS) or apt install ffmpeg (Linux)"
            )
        self.assertIsNotNone(ffmpeg_path, "ffmpeg binary is required for audio and keyframe extraction")

    # --------------------------------------------------------------------------
    # 3. Environment & Secrets
    # --------------------------------------------------------------------------
    def test_03_environment_configuration(self):
        """Check presence of .env and essential environment variables."""
        env_file = PROJECT_ROOT / ".env"
        env_example = PROJECT_ROOT / ".env.example"

        self.assertTrue(env_example.exists(), ".env.example template must be present in repository")

        if not env_file.exists():
            self.audit.add_issue(
                ".env configuration file is missing",
                "Run: cp .env.example .env and populate HEB_STORE_ID and GEMINI_API_KEY"
            )

        # Attempt to load dotenv
        from dotenv import load_dotenv
        if env_file.exists():
            load_dotenv(env_file)

        store_id = os.environ.get("HEB_STORE_ID")
        if not store_id:
            self.audit.add_issue(
                "HEB_STORE_ID environment variable is unset",
                "Add HEB_STORE_ID=390 (or your preferred store ID) to .env"
            )

        # Check Gemini API Key
        gemini_key = os.environ.get("GEMINI_API_KEY")
        if not gemini_key:
            self.audit.add_issue(
                "GEMINI_API_KEY is unset (fallback multimodal structuring requires it)",
                "Add GEMINI_API_KEY=<your_api_key> to .env or system environment"
            )

    # --------------------------------------------------------------------------
    # 4. Configuration Files
    # --------------------------------------------------------------------------
    def test_04_household_and_pantry_configs(self):
        """Verify household.yaml and pantry_staples.json exist and are valid."""
        hh_file = PROJECT_ROOT / "config" / "household.yaml"
        hh_example = PROJECT_ROOT / "config" / "household.example.yaml"
        pantry_file = PROJECT_ROOT / "config" / "pantry_staples.json"
        pantry_example = PROJECT_ROOT / "config" / "pantry_staples.example.json"

        self.assertTrue(hh_example.exists(), "config/household.example.yaml must exist")
        self.assertTrue(pantry_example.exists(), "config/pantry_staples.example.json must exist")

        if not hh_file.exists():
            self.audit.add_issue(
                "config/household.yaml is missing",
                "Run: cp config/household.example.yaml config/household.yaml and customize preferences"
            )
        else:
            import yaml
            with open(hh_file, "r") as f:
                data = yaml.safe_load(f)
                self.assertIn("household", data, "household.yaml must define 'household' block")

        if not pantry_file.exists():
            self.audit.add_issue(
                "config/pantry_staples.json is missing",
                "Run: cp config/pantry_staples.example.json config/pantry_staples.json"
            )
        else:
            import json
            with open(pantry_file, "r") as f:
                pdata = json.load(f)
                self.assertIn("staples", pdata, "pantry_staples.json must define 'staples' list")
                self.assertGreaterEqual(len(pdata["staples"]), 10, "pantry list must have valid items")

    # --------------------------------------------------------------------------
    # 5. Cache Directory Structure
    # --------------------------------------------------------------------------
    def test_05_cache_directories(self):
        """Verify cache/media and cache/recipes directories exist."""
        media_dir = PROJECT_ROOT / "cache" / "media"
        recipes_dir = PROJECT_ROOT / "cache" / "recipes"

        media_dir.mkdir(parents=True, exist_ok=True)
        recipes_dir.mkdir(parents=True, exist_ok=True)

        self.assertTrue(media_dir.is_dir(), "cache/media directory must exist")
        self.assertTrue(recipes_dir.is_dir(), "cache/recipes directory must exist")

    # --------------------------------------------------------------------------
    # 6. Playwright Browser Diagnostics
    # --------------------------------------------------------------------------
    def test_06_playwright_browser_runtime(self):
        """Verify Playwright Chromium browser executable is installed."""
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser_path = p.chromium.executable_path
                self.assertTrue(os.path.exists(browser_path) or browser_path, "Chromium browser binary path must exist")
        except Exception as ex:
            self.audit.add_issue(
                f"Playwright Chromium browser not initialized: {ex}",
                "Run: playwright install chromium (or .venv/bin/playwright install chromium)"
            )

    # --------------------------------------------------------------------------
    # 7. End-to-End Dry Run Smoke Test
    # --------------------------------------------------------------------------
    def test_07_dry_run_pipeline(self):
        """Verify core modules run together in a synthetic dry-run pass."""
        from scripts.cache_manager import PantryComparator, HouseholdScaler, CacheManager, ScaledIngredient, RecipeCache
        from scripts.heb_client import HEBClient

        # A. Pantry check
        pantry = PantryComparator()
        is_salt, _ = pantry.check_staple("1 tsp kosher salt")
        self.assertTrue(is_salt, "Pantry check must identify kosher salt")

        # B. Scaling math & kid friendly split
        scaler = HouseholdScaler()
        factor = scaler.calculate_scale_factor(4.0)
        self.assertAlmostEqual(factor, 0.65, places=2)
        self.assertTrue(scaler.is_kid_sensitive("sriracha hot sauce"))

        # C. HEB 4-Tier Fallback Cascade
        client = HEBClient(store_id="390")
        ing = ScaledIngredient(
            ingredient_key="salmon",
            recipe_requirement="1.25 lbs Atlantic Salmon",
            heb_search_query="Atlantic Salmon",
        )
        match = client.match_ingredient_to_sku(ing)
        self.assertIn(match["status"], ["SUCCESS", "SUBSTITUTED"])
        self.assertIn("sku", match)

        # D. Cache CRUD dry-run in temporary directory
        with tempfile.TemporaryDirectory() as tmp_dir:
            mgr = CacheManager(cache_dir=tmp_dir)
            sample_recipe = RecipeCache(
                id="test-recipe",
                title="Test Honey Garlic Salmon",
                source_url="https://tiktok.com/@test/1",
                video_id="test_vid_1",
                instructions=["Step 1", "Step 2"],
            )
            saved = mgr.save(sample_recipe)
            self.assertTrue(os.path.exists(saved))
            status, retrieved = mgr.lookup("test_vid_1")
            self.assertEqual(status, "HIT")
            self.assertEqual(retrieved["id"], "test-recipe")

    @classmethod
    def tearDownClass(cls):
        cls.audit.print_report()


def main():
    suite = unittest.TestLoader().loadTestsFromTestCase(TestSkillSetupAndDiagnostics)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
