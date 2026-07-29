#!/bin/bash
# Rebuild the swim-1k dashboard from a fresh Apple Health export.
# Usage: ./update.sh "/path/to/Apple Health export.zip"
set -euo pipefail
cd "$(dirname "$0")"
ZIP="${1:?Usage: ./update.sh <path-to-Apple-Health-export.zip>}"
python3 scripts/parse_health.py "$ZIP"
python3 scripts/build_dashboard.py
echo "Done → $(pwd)/index.html"
