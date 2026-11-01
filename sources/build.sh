#!/usr/bin/env bash
# Build Rondel: TTF + WOFF2 for all three styles, then the specimen page.
# Run from anywhere:  sources/build.sh
set -euo pipefail
cd "$(dirname "$0")/.."
python3 sources/build_rondel.py --ttf fonts/ttf --woff2 fonts/webfonts
python3 sources/build_specimen.py --fonts fonts/webfonts --ttf fonts/ttf --out documentation/specimen/index.html
python3 sources/tools/check_monograms.py fonts/ttf
