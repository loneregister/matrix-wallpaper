#!/usr/bin/env python3
"""
gen-atlas.py -- bake the Matrix digital-rain shader (matrix.glsl).

WHAT THIS PRODUCES
------------------
A single self-contained mpv user shader, `matrix.glsl`, that draws falling
"digital rain" using the real Matrix glyph set: mirrored half-width katakana
plus digits. The glyph artwork is rasterized from a TrueType/TTC font *here, at
build time*, and embedded directly into the shader as integer constants. The
shader therefore needs no external texture and no font at runtime -- only mpv.

WHY CONSTANTS INSTEAD OF A TEXTURE
----------------------------------
mpv supports `//!TEXTURE` blocks for user shaders, but on some NVIDIA + mpv
combinations they fail to LINK ("error C3001: no program defined"). Baking the
glyph atlas into a plain `const uint[]` array avoids textures entirely and links
reliably.

THE PIXEL FORMAT (4-bit coverage)
---------------------------------
Each glyph is a GW x GH grayscale bitmap. We keep the font rasterizer's
anti-aliasing instead of hard-thresholding to 1-bit (which looks blocky when
upscaled). Each pixel is stored as 4-bit coverage (0..15 = 16 gray levels), and
EIGHT pixels are packed into each 32-bit `uint` (8 * 4 = 32 bits). The shader
bilinearly samples this field, so glyph edges stay smooth even on a 6K panel.

  - 4-bit (vs 8-bit) halves the array. That matters: the NVIDIA GLSL compiler
    can only bind a `const` array up to a certain size. An 8-bit atlas at 16x26
    (6656 uints) fails with "error C5041 ... Possibly large array"; the shader
    then never links and mpvpaper shows a blank (often blue) surface.
  - RULE OF THUMB: keep the array under ~3500 uints. Size = COUNT * GH * (GW/8).
    The defaults below are 64 * 24 * 2 = 3072 uints, which links fine.

REGENERATING
------------
Tweak the knobs below (GW/GH/RENDER_PX/COUNT/charset) and re-run:

    python3 gen-atlas.py        # rewrites matrix.glsl next to this script

Point at a different font with the MATRIX_FONT env var. After ANY size change,
re-validate that the shader still links before relaunching the wallpaper:

    mpv --no-config --vo=gpu --gpu-context=wayland --glsl-shaders=./matrix.glsl \\
        --msg-level=all=v av://lavfi:color=c=black:s=320x240:r=30 2>&1 \\
      | grep -iE 'error C|Possibly large|shader link log'

(No `shader link log (status=0)` line means it linked.)
"""

import os
from PIL import Image, ImageDraw, ImageFont

# --------------------------------------------------------------------------- #
# Build-time configuration (the only things you normally touch)
# --------------------------------------------------------------------------- #

# Font used to rasterize the glyphs. Must contain half-width katakana (U+FF66..).
# Only needed HERE, when regenerating -- the runtime uses the baked atlas.
FONT = os.environ.get(
    "MATRIX_FONT",
    "/usr/share/fonts/google-noto-sans-mono-cjk-vf-fonts/NotoSansMonoCJK-VF.ttc",
)

GW, GH = 16, 24        # baked glyph cell size in pixels. GW MUST be a multiple
                       # of 8 (8 four-bit pixels pack into each 32-bit uint).
RENDER_PX = 20         # font size (px) used when drawing the glyph into the cell.
                       # A little smaller than GH leaves breathing room.
COUNT = 64             # number of glyphs to bake (also the shader's selection set).

# Output path: matrix.glsl, written alongside this script.
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "matrix.glsl")

# --------------------------------------------------------------------------- #
# Derived values / sanity checks
# --------------------------------------------------------------------------- #

assert GW % 8 == 0, "GW must be a multiple of 8 (8 four-bit nibbles per uint)"
PACK = GW // 8         # number of uints needed to store one glyph row

# --------------------------------------------------------------------------- #
# Choose the character set: 56 half-width katakana + the 10 digits, then trim
# to COUNT. This mirrors the film's glyph palette.
# --------------------------------------------------------------------------- #

katakana = [chr(c) for c in range(0xFF66, 0xFF9D + 1)]   # 56 half-width katakana
digits = [str(d) for d in range(10)]                     # '0'..'9'
glyphs = (katakana + digits)[:COUNT]
assert len(glyphs) == COUNT, f"need {COUNT} glyphs, have {len(glyphs)}"

# --------------------------------------------------------------------------- #
# Rasterize each glyph and pack it into 4-bit coverage words.
#
# We render at SS x the target size and downsample with Lanczos. Drawing big
# then shrinking gives much cleaner anti-aliased coverage than rasterizing
# directly into a tiny GW x GH cell.
# --------------------------------------------------------------------------- #

SS = 4                                       # supersampling factor
font = ImageFont.truetype(FONT, RENDER_PX * SS)

words = []   # flat list: COUNT * GH * PACK uints, row-major per glyph
for ch in glyphs:
    # Draw the glyph centered in an oversized (GW*SS x GH*SS) grayscale canvas.
    big = Image.new("L", (GW * SS, GH * SS), 0)
    draw = ImageDraw.Draw(big)
    bbox = draw.textbbox((0, 0), ch, font=font)            # ink bounds of glyph
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (GW * SS - w) / 2 - bbox[0]                         # center horizontally
    y = (GH * SS - h) / 2 - bbox[1]                         # center vertically
    draw.text((x, y), ch, fill=255, font=font)

    # Downsample to the real cell size, then mirror left<->right. In the film
    # the glyphs are flipped; the source font is not, so we flip here.
    cell = big.resize((GW, GH), Image.LANCZOS)
    cell = cell.transpose(Image.FLIP_LEFT_RIGHT)
    px = cell.load()

    # Pack each row: 8 pixels -> one uint. Pixel j occupies bits [j*4 .. j*4+3].
    # The 8-bit gray value is reduced to 4 bits via >> 4 (0..255 -> 0..15).
    for gy in range(GH):
        for gx0 in range(0, GW, 8):
            packed = 0
            for j in range(8):
                nib = px[gx0 + j, gy] >> 4                 # 0..15 coverage
                packed |= (nib & 0xF) << (j * 4)
            words.append(packed)

assert len(words) == COUNT * GH * PACK
glyph_data = ", ".join(f"{v}u" for v in words)             # GLSL uint literals

# --------------------------------------------------------------------------- #
# Emit the shader. This is an mpv user shader (hooks the MAIN texture). Almost
# everything below is GLSL; only {GW}/{GH}/{PACK}/{COUNT}/{glyph_data} are
# substituted. Note: literal GLSL braces are doubled ({{ }}) for str.format/
# f-string escaping.
# --------------------------------------------------------------------------- #

shader = f"""//!HOOK MAIN
//!BIND HOOKED
//!DESC matrix digital rain (real glyphs, 4-bit coverage)

// ===================== tunables (safe to tweak live) ===================== //
#define ROWS      60.0           // glyph rows down the screen; higher = denser
#define TRAIL_MIN 12.0           // shortest trail (in glyph-rows)
#define TRAIL_MAX 36.0           // longest trail
#define SPEED_MIN 3.0            // slowest fall speed (glyph-rows per second)
#define SPEED_MAX 26.0           // fastest fall speed; wide gap => clear overtaking
#define DROPS       3            // independent drops per column (enables overtaking)
#define DROP_CHANCE 0.75         // fraction of drop-slots that are active
                                 //   1.0 = every column always full; lower = lighter
#define HEAD_COL  vec3(0.80, 1.00, 0.85)   // leading glyph (near white-green)
#define BODY_COL  vec3(0.10, 0.95, 0.28)   // classic trailing matrix green
#define FPS       60.0           // must match the lavfi source frame rate

// --- baked atlas geometry (set by gen-atlas.py; do not hand-edit) --- //
#define GW        {GW}             // glyph cell width  (px)
#define GH        {GH}             // glyph cell height (px)
#define PACK      {PACK}              // uints per glyph row (GW / 8)
#define GLYPH_COUNT {COUNT}.0

// Orientation fixes: flip to 1 if the rain falls UPWARD (FLIP_Y) or the
// individual glyphs render upside-down (ATLAS_FLIP_V).
#define FLIP_Y       0
#define ATLAS_FLIP_V 0
// ========================================================================= //

// The glyph atlas: COUNT glyphs, GH rows each, PACK uints per row. Each uint
// holds 8 pixels of 4-bit (0..15) coverage; pixel j is in bits [j*4 .. j*4+3].
const uint GLYPHS[{COUNT} * {GH} * {PACK}] = uint[]({glyph_data});

// -- cheap hash helpers: deterministic pseudo-random in [0,1) -- //
// hash11: 1D -> 1D. Used to give each column/drop stable random attributes.
float hash11(float p){{ p = fract(p*0.1031); p *= p+33.33; p *= p+p; return fract(p); }}
// hash21: 2D -> 1D. Used to pick which glyph shows in a given cell.
float hash21(vec2 p){{
    vec3 p3 = fract(vec3(p.xyx) * vec3(0.1031, 0.1030, 0.0973));
    p3 += dot(p3, p3.yzx + 33.33);
    return fract((p3.x + p3.y) * p3.z);
}}

// Coverage of pixel (xi,yi) of glyph `idx`, returned in [0,1]. Out-of-range
// reads return 0 so bilinear sampling fades cleanly at the cell border.
float cov(int idx, int xi, int yi){{
    if (xi < 0 || xi >= GW || yi < 0 || yi >= GH) return 0.0;
    uint word = GLYPHS[(idx * GH + yi) * PACK + (xi >> 3)]; // which uint holds it
    uint shift = uint((xi & 7) * 4);                       // nibble within the uint
    return float((word >> shift) & 0xFu) / 15.0;           // 4-bit -> [0,1]
}}

// Bilinearly sample glyph `idx` at cell-local coord p (each axis in [0,1]).
// Sampling between the 4-bit texels is what makes the edges look smooth.
float sampleGlyph(int idx, vec2 p){{
    float fx = p.x * float(GW) - 0.5;   // to texel space, -0.5 centers the texel
    float fy = p.y * float(GH) - 0.5;
    int x0 = int(floor(fx)), y0 = int(floor(fy));
    float tx = fx - float(x0), ty = fy - float(y0);        // bilinear weights
    float a = cov(idx, x0,   y0);
    float b = cov(idx, x0+1, y0);
    float c = cov(idx, x0,   y0+1);
    float d = cov(idx, x0+1, y0+1);
    return mix(mix(a, b, tx), mix(c, d, tx), ty);
}}

vec4 hook(){{
    float time = float(frame) / FPS;     // seconds since start (frame is mpv-provided)
    vec2  res  = HOOKED_size;            // render target size in pixels
    vec2  uv   = HOOKED_pos;             // normalized [0,1] fragment position
#if FLIP_Y
    uv.y = 1.0 - uv.y;
#endif
    vec2  frag = uv * res;               // fragment position in pixels

    // Build the glyph grid. Cells are ROWS tall; width is 0.62 of the height to
    // match the half-width katakana aspect. cellW is then rounded so an integer
    // number of columns spans the screen exactly (no seam at the right edge).
    float cellH = res.y / ROWS;
    float cellW = cellH * 0.62;
    float cols  = floor(res.x / cellW);
    cellW = res.x / cols;

    float col = floor(frag.x / cellW);   // this fragment's column index
    float row = floor(frag.y / cellH);   // ...and row index

    // ---- rain intensity at this cell ----
    // Each column hosts DROPS independent falling heads, each with its own
    // speed, trail length and phase. We evaluate all of them and keep the
    // BRIGHTEST, so a fast drop visibly overtakes and passes through a slower
    // one sharing the column. DROP_CHANCE leaves some slots dormant for a
    // lighter, less uniform downpour.
    float bright = 0.0;
    float dHead  = 1e9;                  // distance-below-head of the winning drop
    for (int i = 0; i < DROPS; i++) {{
        float fi = float(i);
        // Per-column, per-slot activation: skip dormant slots.
        if (hash11(col*4.1 + 11.0 + fi*23.3) > DROP_CHANCE) continue;

        // Stable random attributes for this (column, slot). The large, distinct
        // per-slot offsets (23.3 / 31.7 / 17.3 / 53.1) decorrelate the slots so
        // their speeds really differ -- that spread is what you see as variety.
        float sp  = mix(SPEED_MIN, SPEED_MAX, hash11(col*1.3 + 7.0 + fi*31.7));
        float tr  = mix(TRAIL_MIN, TRAIL_MAX, hash11(col*2.7 + 3.0 + fi*17.3));
        float off = hash11(col*0.7 + 1.0 + fi*53.1) * 1000.0;   // phase offset

        // Head position marches down at `sp` rows/sec. `per` is the full cycle
        // (visible screen + trail) so a drop fully exits before it reappears.
        // di = how far THIS cell sits below the head, wrapped into the cycle.
        float hd  = time * sp + off;
        float per = ROWS + tr;
        float di  = mod(hd - row, per);

        // Inside the trail? Brightness falls off from the head (di=0) to the
        // tail (di=tr). pow(...,1.4) makes the falloff a touch steeper.
        if (di >= 0.0 && di < tr) {{
            float b = pow(1.0 - di / tr, 1.4);
            if (b > bright) {{ bright = b; dHead = di; }}    // keep brightest
        }}
    }}
    if (bright <= 0.0) return vec4(0.0, 0.0, 0.0, 1.0);      // dark cell: nothing here

    // ---- which glyph, drawn smoothly ----
    vec2 inCell = vec2(fract(frag.x / cellW), fract(frag.y / cellH));  // [0,1] in cell
#if ATLAS_FLIP_V
    inCell.y = 1.0 - inCell.y;
#endif
    // Glyphs occasionally morph: glyphId steps over time at a per-cell rate, so
    // each cell flickers through different characters as the rain passes.
    float glyphId = floor(time * (2.0 + 6.0 * hash11(col*5.0 + row*0.7))
                          + row*1.7 + col*0.3);
    int idx = int(clamp(floor(hash21(vec2(col + glyphId*3.1, row - glyphId*1.7))
                              * GLYPH_COUNT), 0.0, GLYPH_COUNT - 1.0));
    float lit = sampleGlyph(idx, inCell);                   // glyph coverage [0,1]

    // The leading glyph (within ~1 row of the head) is near-white; the trail is
    // green. Modulate by brightness (trail falloff) and glyph coverage.
    float isHead = step(dHead, 1.0);
    vec3 glyphCol = mix(BODY_COL, HEAD_COL, isHead);

    vec3 outc = glyphCol * bright * lit;
    outc += BODY_COL * bright * 0.04;        // faint green wash along the column

    return vec4(outc, 1.0);
}}
"""

# --------------------------------------------------------------------------- #
# Write it out and report the atlas size (watch the ~3500-uint link ceiling).
# --------------------------------------------------------------------------- #

with open(OUT, "w") as f:
    f.write(shader)

print(f"wrote {OUT}: {COUNT} glyphs {GW}x{GH} 4-bit coverage "
      f"({len(words)} uints, {PACK}/row), pure-GLSL (no //!TEXTURE)")
