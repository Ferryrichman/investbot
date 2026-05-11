#!/usr/bin/env bash
# Build dist/ for GitHub Pages deployment
# Structure:
#   dist/              ← landing page (root)
#   dist/pe/           ← 啤 Monitor dashboard
#   dist/rights/       ← (future) 供股配股 GO
#   dist/junk/         ← (future) 絕L Monitor
set -e

rm -rf dist
mkdir -p dist/pe

# Landing page at root
cp -r landing/* dist/

# 啤 Monitor under /pe/
cp frontend/index.html dist/pe/
cp frontend/style.css dist/pe/
cp frontend/app.js dist/pe/
cp frontend/crypto.js dist/pe/

# Public preview (always present)
if [ -f frontend/data_preview.json ]; then
  cp frontend/data_preview.json dist/pe/
fi

# Encrypted blob (gated)
if [ -f frontend/data.enc.json ]; then
  cp frontend/data.enc.json dist/pe/
fi

# Note: frontend/data.json is gitignored at deploy — never publicly served
echo "Built dist/ structure:"
find dist -maxdepth 3 -type f
