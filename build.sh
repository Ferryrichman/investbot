#!/usr/bin/env bash
# Build dist/ for GitHub Pages deployment
# Structure:
#   dist/              ← landing page (root)
#   dist/pe/           ← 啤 Monitor dashboard
#   dist/caitech/          ← 港股財技 section landing
#   dist/caitech/holying/  ← 好L型財技 dashboard
#   dist/rights/           ← (future) 供股配股 GO
set -e

rm -rf dist
mkdir -p dist/caitech/holying

# Landing page at root
cp -r landing/* dist/

# 港股財技 section + 好L型財技
cp caitech/index.html dist/caitech/
cp caitech/holying/index.html dist/caitech/holying/

# Note: frontend/data.json is gitignored at deploy — never publicly served
echo "Built dist/ structure:"
find dist -maxdepth 3 -type f
