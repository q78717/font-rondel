#!/usr/bin/env bash
# Build Rondel in one command:  sources/build.sh
#   1. sources/build_rondel.py draws every glyph from arcs and lines and writes the UFO sources
#   2. gftools builder compiles, hints and checks them into fonts/ttf and fonts/webfonts
#   3. the specimen page is rebuilt and the monogram feature is smoke-tested
set -euo pipefail
cd "$(dirname "$0")/.."
python3 sources/build_rondel.py --ufo sources/ufo
(cd sources && for cfg in config.yaml config-ring.yaml config-coin.yaml; do gftools builder "$cfg"; done)
python3 sources/tools/add_stat.py fonts/ttf fonts/webfonts
python3 sources/build_specimen.py --fonts fonts/webfonts --ttf fonts/ttf --out documentation/specimen/index.html
python3 sources/tools/check_monograms.py fonts/ttf
