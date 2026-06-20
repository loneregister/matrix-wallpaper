#!/usr/bin/env bash
# Matrix digital-rain animated wallpaper, via mpvpaper + a GLSL shader.
#
# Renders the generative shader (matrix.glsl) over a black lavfi source on the
# `bottom` wlr-layer-shell layer: above a static wallpaper, below windows/bar.
# Requires a wlroots-based compositor (Hyprland, Sway, river, ...).
#
# Usage:
#   ./start-matrix.sh <output> [WxH]
#   ./start-matrix.sh DP-3                 # auto-detects that output's resolution
#   ./start-matrix.sh DP-3 2560x1440       # force a resolution
#   MATRIX_FPS=30 ./start-matrix.sh eDP-1  # override frame rate (default 60)
#
# Tip: match WxH to the output's real pixel size so the shader renders 1:1 and
# stays crisp (mpvpaper upscales a smaller render, which softens the glyphs).
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PATH="$HOME/.local/bin:$PATH"      # mpvpaper is often a ~/.local install
SHADER="$DIR/matrix.glsl"
FPS="${MATRIX_FPS:-60}"

OUTPUT="${1:-${MATRIX_OUTPUT:-}}"
if [[ -z "$OUTPUT" ]]; then
  echo "Usage: $0 <output> [WxH]" >&2
  if command -v hyprctl >/dev/null 2>&1; then
    echo "Available outputs:" >&2
    hyprctl monitors -j | python3 -c '
import sys, json
for m in json.load(sys.stdin):
    print(f"  {m[\"name\"]}  {m[\"width\"]}x{m[\"height\"]}")' 2>/dev/null || true
  fi
  exit 1
fi

# Resolution: arg2, then $MATRIX_RES, then auto-detect via hyprctl, else 1080p.
RES="${2:-${MATRIX_RES:-}}"
if [[ -z "$RES" ]] && command -v hyprctl >/dev/null 2>&1; then
  RES="$(hyprctl monitors -j | MATRIX_O="$OUTPUT" python3 -c '
import sys, json, os
o = os.environ["MATRIX_O"]
for m in json.load(sys.stdin):
    if m["name"] == o:
        print(f"{m[\"width\"]}x{m[\"height\"]}"); break' 2>/dev/null || true)"
fi
RES="${RES:-1920x1080}"

if ! command -v mpvpaper >/dev/null 2>&1; then
  echo "error: mpvpaper not found in PATH (build it -- see README)." >&2
  exit 1
fi

# Replace any previous instance on this output.
pkill -f "mpvpaper.*${OUTPUT}" 2>/dev/null || true
sleep 0.3

echo "matrix: output=$OUTPUT res=$RES fps=$FPS shader=$SHADER"

# -f forks/daemonizes so this returns to the shell.
exec mpvpaper -v -f -l bottom \
  -o "no-audio no-osc loop-file=inf keep-open=yes hwdec=no \
      background=color background-color=#000000 \
      glsl-shaders=${SHADER}" \
  "${OUTPUT}" "av://lavfi:color=c=black:s=${RES}:r=${FPS}"
