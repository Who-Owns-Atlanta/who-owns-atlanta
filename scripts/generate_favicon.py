#!/usr/bin/env python3
"""
Generate Atlanta poverty-map favicon and app icons.
The Atlanta city outline (with poverty-colored neighborhoods) resembles an "A".
"""

import json
import math
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import PathPatch
from matplotlib.path import Path
from PIL import Image
import io
import os

# ── Load data ──────────────────────────────────────────────────────────────
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GEO_DIR = os.path.join(BASE, "web/frontend/geojson")

city_limits  = json.load(open(os.path.join(GEO_DIR, "atlanta_city_limits.json")))
neighborhoods = json.load(open(os.path.join(GEO_DIR, "neighborhood_demographics.json")))

# ── Mercator correction at Atlanta's latitude ──────────────────────────────
LAT_CENTER = 33.76   # approx center of Atlanta
# 1° lon ≈ cos(lat) * (1° lat in km).  To display correctly, stretch x by 1/cos(lat).
MERC_ASPECT = 1.0 / math.cos(math.radians(LAT_CENTER))   # ≈ 1.202

# ── Color mapping (YlOrRd — matches the poverty map) ──────────────────────
POVERTY_STOPS = [
    (0.00, "#FAFFA0"),
    (0.05, "#FFED6A"),
    (0.12, "#FDC144"),
    (0.20, "#FD8D3C"),
    (0.30, "#F03B20"),
    (0.40, "#C0141B"),
    (0.55, "#800010"),
    (1.00, "#4A0008"),
]

def hex_to_rgb(h):
    h = h.lstrip('#')
    return tuple(int(h[i:i+2], 16)/255 for i in (0, 2, 4))

STOPS = [(v, hex_to_rgb(c)) for v, c in POVERTY_STOPS]

def poverty_color(pct):
    t = min(max(pct / 100.0, 0.0), 1.0)
    for i in range(len(STOPS) - 1):
        v0, c0 = STOPS[i]
        v1, c1 = STOPS[i + 1]
        if v0 <= t <= v1:
            f = (t - v0) / (v1 - v0)
            return tuple(c0[j] + f * (c1[j] - c0[j]) for j in range(3))
    return STOPS[-1][1]

# ── GeoJSON → matplotlib Path ──────────────────────────────────────────────
def polygon_to_path(coords_rings):
    verts, codes = [], []
    for ring in coords_rings:
        if not ring:
            continue
        verts.append(ring[0])
        codes.append(Path.MOVETO)
        for pt in ring[1:]:
            verts.append(pt)
            codes.append(Path.LINETO)
        verts.append(ring[0])
        codes.append(Path.CLOSEPOLY)
    return Path(verts, codes)

# ── Build figure ───────────────────────────────────────────────────────────
def build_icon(size_px=512, padding_frac=0.04):
    """
    Render at size_px × size_px with transparent background.
    Returns a PIL RGBA Image.
    """
    # Bounding box from ALL city polygons
    all_pts = []
    for poly in city_limits['features'][0]['geometry']['coordinates']:
        for ring in poly:
            all_pts.extend(ring)
    lons = [p[0] for p in all_pts]
    lats = [p[1] for p in all_pts]
    lon_min, lon_max = min(lons), max(lons)
    lat_min, lat_max = min(lats), max(lats)

    # Padding in data coords
    pad_lon = (lon_max - lon_min) * padding_frac
    pad_lat = (lat_max - lat_min) * padding_frac
    lon_min -= pad_lon; lon_max += pad_lon
    lat_min -= pad_lat; lat_max += pad_lat

    # Convert to Mercator-corrected display coords (multiply lon by MERC_ASPECT
    # so that 1 display unit = same real distance on both axes).
    # Everything stays in lon/lat space; we just tell matplotlib what aspect to use.
    # matplotlib set_aspect(r) means: 1 y-unit takes r× as much screen space as 1 x-unit.
    # We want 1° lat to appear MERC_ASPECT× taller than 1° lon is wide → aspect=MERC_ASPECT.

    # After padding, work out which axis is the limiting one so the shape fills
    # the square canvas correctly.
    lon_span_disp = (lon_max - lon_min)               # x in data units
    lat_span_disp = (lat_max - lat_min) * MERC_ASPECT # y in "display equivalent" units

    if lon_span_disp > lat_span_disp:
        # wider than tall → add lat padding to centre vertically
        extra = (lon_span_disp - lat_span_disp) / MERC_ASPECT / 2
        lat_min -= extra; lat_max += extra
    else:
        # taller than wide → add lon padding
        extra = (lat_span_disp - lon_span_disp) / 2
        lon_min -= extra; lon_max += extra

    dpi = 96
    fig_size = size_px / dpi
    fig, ax = plt.subplots(figsize=(fig_size, fig_size), dpi=dpi)
    # TRANSPARENT background
    fig.patch.set_alpha(0)
    ax.set_facecolor((0, 0, 0, 0))

    ax.set_xlim(lon_min, lon_max)
    ax.set_ylim(lat_min, lat_max)
    ax.set_aspect(MERC_ASPECT)   # correct Mercator distortion
    ax.axis('off')
    fig.subplots_adjust(left=0, right=1, bottom=0, top=1)

    # ── Step 1: Neighborhood patches colored by poverty ────────────────────
    for feat in neighborhoods['features']:
        pct = feat['properties'].get('poverty_pct') or 0
        color = poverty_color(pct)
        geom = feat['geometry']
        rings_list = [geom['coordinates']] if geom['type'] == 'Polygon' \
                     else geom['coordinates']
        for poly in rings_list:
            patch = PathPatch(polygon_to_path(poly),
                              facecolor=color, edgecolor='none', linewidth=0, zorder=2)
            ax.add_patch(patch)

    # ── Step 2: Subtle internal neighborhood lines ─────────────────────────
    if size_px >= 192:
        line_lw = 0.18 if size_px >= 512 else 0.12
        for feat in neighborhoods['features']:
            geom = feat['geometry']
            rings = geom['coordinates'] if geom['type'] == 'Polygon' else \
                    [r for poly in geom['coordinates'] for r in poly]
            for ring in rings:
                xs = [p[0] for p in ring]
                ys = [p[1] for p in ring]
                ax.plot(xs, ys, color='#1A0005', linewidth=line_lw, alpha=0.28, zorder=3)

    # ── Step 3: City boundary — gold rim over dark shadow ─────────────────
    city_geom = city_limits['features'][0]['geometry']
    stroke_lw = max(1.2, size_px / 160)
    # Only the main (largest) polygon; skip tiny enclave
    polys_sorted = sorted(city_geom['coordinates'], key=lambda p: len(p[0]), reverse=True)
    for poly in polys_sorted[:1]:
        for ring in poly:
            xs = [p[0] for p in ring]
            ys = [p[1] for p in ring]
            ax.plot(xs, ys, color='#000000', linewidth=stroke_lw * 3.2,
                    alpha=0.65, zorder=4,
                    solid_capstyle='round', solid_joinstyle='round')
            ax.plot(xs, ys, color='#1E4C8A', linewidth=stroke_lw,
                    alpha=1.0, zorder=5,
                    solid_capstyle='round', solid_joinstyle='round')

    # ── Render to PIL RGBA (transparent bg) ───────────────────────────────
    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=dpi,
                transparent=True, bbox_inches='tight', pad_inches=0)
    plt.close(fig)
    buf.seek(0)
    img = Image.open(buf).convert('RGBA')
    img = img.resize((size_px, size_px), Image.LANCZOS)

    # PIL radial vignette: darkens (not blackens) the corners
    cx_px, cy_px = img.size[0] / 2, img.size[1] / 2
    max_r = math.sqrt(cx_px**2 + cy_px**2)
    vignette = Image.new('L', img.size, 0)
    pixels = vignette.load()
    for y in range(img.size[1]):
        for x in range(img.size[0]):
            t = math.sqrt((x - cx_px)**2 + (y - cy_px)**2) / max_r
            darkness = int(max(0.0, (t - 0.60) / 0.40) ** 1.8 * 140)
            pixels[x, y] = darkness
    dark_layer = Image.new('RGBA', img.size, (0, 0, 0, 0))
    dark_layer.putalpha(vignette)
    img = Image.alpha_composite(img, dark_layer)

    return img


# ── Output ─────────────────────────────────────────────────────────────────
OUT_DIR = os.path.join(BASE, "web/frontend/icons")
os.makedirs(OUT_DIR, exist_ok=True)

print("Generating 512px icon…")
img512 = build_icon(512)
img512.save(os.path.join(OUT_DIR, "icon-512.png"))
print("  → icon-512.png")

print("Generating 192px icon…")
img192 = build_icon(192)
img192.save(os.path.join(OUT_DIR, "icon-192.png"))
print("  → icon-192.png")

print("Generating favicons (from 256px source)…")
src = build_icon(256)
img48 = src.resize((48, 48), Image.LANCZOS)
img32 = src.resize((32, 32), Image.LANCZOS)
img16 = src.resize((16, 16), Image.LANCZOS)
img32.save(os.path.join(OUT_DIR, "favicon-32.png"))
img16.save(os.path.join(OUT_DIR, "favicon-16.png"))
print("  → favicon-32.png / favicon-16.png")

ico_path = os.path.join(BASE, "web/frontend/favicon.ico")
img48.save(ico_path, format='ICO', sizes=[(16,16),(32,32),(48,48)],
           append_images=[img32, img16])
print(f"  → favicon.ico")

print("\nDone.")
