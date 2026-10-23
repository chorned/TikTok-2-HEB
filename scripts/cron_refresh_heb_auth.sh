#!/bin/bash
set -euo pipefail

# Resolve repository root dynamically so it works whether cloned as heb-mcp.skill or TikTok-2-HEB
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

# Set PATH for headless execution
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin"

# Run with virtualenv python
exec "$REPO_DIR/.venv/bin/python" "$REPO_DIR/scripts/cron_refresh_heb_auth.py" "$@"
