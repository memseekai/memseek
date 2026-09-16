#!/usr/bin/env bash
# Regenerates the derived assets in public/ from the redesign's originals.
#
# The derived files are committed, so this only needs running when an original
# changes. Point SRC at a checkout of the redesign package; the originals are
# not vendored here because the PNG alone is 1.5MB and never ships.
#
# Requires: pyftsubset (`uv tool install 'fonttools[woff]'`) and node with the
# project's dependencies installed (sharp).

set -euo pipefail
SRC="${1:?usage: prepare-assets.sh <path to memseek-website-redesign>}"
OUT="$(cd "$(dirname "$0")/.." && pwd)/public"

# DM Sans ships as a 240KB variable TTF carrying both a wght (100-1000) and an
# opsz (9-40) axis. Splitting Latin from Latin-ext means a page of English
# prose pulls 70KB instead of 105KB. Keeping opsz is free: font-optical-sizing
# is auto by default, so the 92px hero and 17px body each get the right cut.
LATIN='U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,U+0329,U+2000-206F,U+2074,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD'
EXT='U+0100-02BA,U+02BD-02C5,U+02C7-02CC,U+02CE-02D7,U+02DD-02FF,U+0304,U+0308,U+0329,U+1D00-1DBF,U+1E00-1E9F,U+1EF2-1EFF,U+2020,U+20A0-20AB,U+20AD-20C0,U+2113,U+2C60-2C7F,U+A720-A7FF'

mkdir -p "$OUT/fonts" "$OUT/apps"

for cut in "latin:$LATIN" "latin-ext:$EXT"; do
  pyftsubset "$SRC/assets/dm-sans-variable.ttf" \
    --output-file="$OUT/fonts/dm-sans-${cut%%:*}.woff2" \
    --flavor=woff2 --layout-features='*' --unicodes="${cut#*:}" \
    --no-hinting --desubroutinize
done
cp "$SRC/assets/OFL.txt" "$OUT/fonts/OFL.txt"

# The app marks identify the example sources in the org graph. Shapes and
# colours are unchanged from each vendor's own published asset; see the
# provenance table in the redesign's assets/apps/README.md. No partnership is
# claimed or implied.
cp "$SRC"/assets/apps/{gmail,hubspot,slack,linear}.svg "$OUT/apps/"

# The glass fold is decorative scenery: .atmosphere-fold paints it at 14-20%
# opacity under a radial mask and mix-blend-mode multiply, never wider than
# ~600 CSS px. The 1536x1024 source is 1.5MB; 1200px of WebP is 25KB and no
# difference is visible through the mask.
node -e "
require('sharp')('$SRC/assets/memseek-glass-fold.png')
  .resize({ width: 1200 }).webp({ quality: 74, effort: 6 })
  .toFile('$OUT/glass-fold.webp')
  .then(i => console.log('glass-fold.webp', i.width + 'x' + i.height, i.size, 'bytes'));
"
