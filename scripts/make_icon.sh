#!/bin/bash
# Build the app icon from assets/icon.svg.
#
#   scripts/make_icon.sh
#
# Writes assets/icon.png (1024 px) and assets/loki.icns. Run it after editing
# the SVG, then rerun scripts/build_app.sh so Loki.app picks up the new icon.
#
# The SVG is the source of truth. Every size is rendered from it directly
# rather than downscaled from one big PNG, so small sizes stay crisp.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SVG="$ROOT/assets/icon.svg"
PNG="$ROOT/assets/icon.png"
ICNS="$ROOT/assets/loki.icns"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

if [ ! -f "$SVG" ]; then
    echo "No SVG at $SVG" >&2
    exit 1
fi

# Pick a rasteriser. rsvg-convert and Inkscape are the most standards-complete;
# the bundled Swift helper needs nothing installed, so it is the fallback.
if command -v rsvg-convert >/dev/null 2>&1; then
    RASTERISER="rsvg-convert"
elif command -v inkscape >/dev/null 2>&1; then
    RASTERISER="inkscape"
elif command -v swift >/dev/null 2>&1; then
    RASTERISER="swift"
else
    echo "Need one of rsvg-convert, inkscape or swift to rasterise the SVG." >&2
    echo "Install the Xcode command line tools:  xcode-select --install" >&2
    exit 1
fi
echo "Rasterising with $RASTERISER"

render() {  # render <pixels> <output>
    local px="$1" out="$2"
    case "$RASTERISER" in
        rsvg-convert) rsvg-convert -w "$px" -h "$px" -o "$out" "$SVG" ;;
        inkscape)     inkscape "$SVG" --export-type=png --export-filename="$out" \
                          -w "$px" -h "$px" >/dev/null 2>&1 ;;
        swift)        swift "$ROOT/scripts/svg2png.swift" "$SVG" "$out" "$px" >/dev/null ;;
    esac
}

ICONSET="$WORK/loki.iconset"
mkdir -p "$ICONSET"

# The names and sizes iconutil expects.
render 16   "$ICONSET/icon_16x16.png"
render 32   "$ICONSET/icon_16x16@2x.png"
cp "$ICONSET/icon_16x16@2x.png" "$ICONSET/icon_32x32.png"
render 64   "$ICONSET/icon_32x32@2x.png"
render 128  "$ICONSET/icon_128x128.png"
render 256  "$ICONSET/icon_128x128@2x.png"
cp "$ICONSET/icon_128x128@2x.png" "$ICONSET/icon_256x256.png"
render 512  "$ICONSET/icon_256x256@2x.png"
cp "$ICONSET/icon_256x256@2x.png" "$ICONSET/icon_512x512.png"
render 1024 "$ICONSET/icon_512x512@2x.png"

cp "$ICONSET/icon_512x512@2x.png" "$PNG"
iconutil --convert icns --output "$ICNS" "$ICONSET"

# The web UI uses the same artwork for its favicon, served from web/.
cp "$SVG" "$ROOT/web/icon.svg"

echo "Wrote $PNG"
echo "Wrote $ICNS"
echo "Wrote $ROOT/web/icon.svg"
echo
echo "Now rebuild the app so it picks the icon up:  scripts/build_app.sh"
