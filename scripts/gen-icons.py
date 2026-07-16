#!/usr/bin/env python3
"""
Regenerate the PWA app-icon family from a single Python composition.

Composes a cream rounded square with the rosa rugosa watercolor centered
as the icon's only artwork. Outputs the four PNG sizes the manifest +
apple-touch-icon link reference, plus a small SVG fallback (cream tile,
no embedded raster).

Run after editing the layout below:
    python3 scripts/gen-icons.py

Inputs:
    - images/rosa-rugosa.png       — the watercolor source (transparent BG)

Outputs:
    - icons/icon-192.png           (192×192, rounded)
    - icons/icon-512.png           (512×512, rounded)
    - icons/icon-maskable-512.png  (512×512, full bleed, content in center 80%)
    - icons/apple-touch-icon.png   (180×180, rounded)
    - icons/icon.svg               (lightweight SVG, cream tile)
"""

import base64
import io
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
ROSA_PATH = ROOT / "images" / "rosa-rugosa.png"
ICONS_DIR = ROOT / "icons"

# Palette — same tokens as the site CSS.
CREAM = (245, 240, 235, 255)        # --bg / --cream    #F5F0EB


def find_visual_center(rosa: Image.Image) -> tuple[int, int]:
    """
    Find the artwork's visual center of mass — the alpha-weighted
    centroid of every opaque pixel. Centering on this rather than the
    opaque bbox or the pink bloom alone gives the most balanced read:
    leaves and bloom both contribute according to how much they cover,
    so the eye sees the composition sitting evenly in the canvas.

    Bloom-centering pulled the rose down because the leaves extend
    below the bloom; bbox-centering let the bloom drift up because
    leaves extend further down than they extend up. Centroid splits
    the difference.
    """
    alpha = rosa.split()[3]
    w, h = alpha.size
    pixels = alpha.load()
    sum_x = 0
    sum_y = 0
    weight = 0
    for y in range(h):
        row_w = 0
        row_x = 0
        for x in range(w):
            a = pixels[x, y]
            if a:
                row_x += x * a
                row_w += a
        sum_x += row_x
        sum_y += y * row_w
        weight += row_w
    if weight == 0:
        return (w // 2, h // 2)
    return (sum_x // weight, sum_y // weight)


def render_canvas(size: int, *, rounded: bool) -> Image.Image:
    """
    Compose one full icon at `size` px square.

    rounded=True clips the canvas to a rounded square (used for the
    "any" purpose icons + apple-touch).
    rounded=False is full-bleed (used for the maskable variant; the
    platform masks the corners). The artwork composition is identical
    across all sizes and both rounded modes — only the canvas shape
    differs — so an installed PWA, an iOS Add-to-Home-Screen, and an
    Android maskable launcher all show the same rose at the same place.

    Render is always done at a fixed CANONICAL square (768 px) regardless
    of `size`, then resized to the target. One canonical render →
    identical layout at every size.
    """
    CANONICAL = 768
    W = CANONICAL
    canvas = Image.new("RGBA", (W, W), (0, 0, 0, 0))

    # Background: filled cream (rounded if requested).
    bg = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    bg_draw = ImageDraw.Draw(bg)
    if rounded:
        radius = int(W * 96 / 512)  # match the existing 96/512 ratio
        bg_draw.rounded_rectangle((0, 0, W, W), radius=radius, fill=CREAM)
    else:
        bg_draw.rectangle((0, 0, W, W), fill=CREAM)
    canvas.alpha_composite(bg)

    # Rosa rugosa watercolor — the only artwork on the icon. Center it
    # on the visual centroid so leaves and bloom contribute equally to
    # the perceived placement, then size so no leaf extends past a small
    # safe margin. The safe margin is generous (10%) so the maskable
    # variant's outer ring (which Android can mask) stays clear of any
    # leaf tips — same artwork looks correct under iOS rounding,
    # Android masking, or as a flat raster.
    rosa = Image.open(ROSA_PATH).convert("RGBA")
    opaque_bbox = rosa.getbbox()
    if opaque_bbox is not None:
        rosa = rosa.crop(opaque_bbox)
    cx, cy = find_visual_center(rosa)

    margin = int(W * 0.10)
    target_half = (W - 2 * margin) // 2
    max_x_extent = max(cx, rosa.width - cx)
    max_y_extent = max(cy, rosa.height - cy)
    scale = min(target_half / max_x_extent, target_half / max_y_extent)
    new_w = int(rosa.width * scale)
    new_h = int(rosa.height * scale)
    rosa = rosa.resize((new_w, new_h), Image.LANCZOS)
    cx = int(cx * scale)
    cy = int(cy * scale)

    canvas_center = W // 2
    rosa_x = canvas_center - cx
    rosa_y = canvas_center - cy
    canvas.alpha_composite(rosa, (rosa_x, rosa_y))

    return canvas.resize((size, size), Image.LANCZOS)


def write_svg(out_path: Path) -> None:
    """
    SVG fallback for PWA install dialogs that prefer SVG over PNG. The
    rosa rugosa watercolor is downsampled to 384 px wide and embedded as
    base64 so the file stays self-contained (~30 KB) without referencing
    external assets — hosts that prefetch only the SVG still render the
    full artwork. The full-resolution source (~800 KB) would balloon the
    SVG to >1 MB after base64 overhead, so we resize first.
    """
    rosa = Image.open(ROSA_PATH).convert("RGBA")
    opaque_bbox = rosa.getbbox()
    if opaque_bbox is not None:
        rosa = rosa.crop(opaque_bbox)
    cx, cy = find_visual_center(rosa)

    # Downsample to a sane embed size so the SVG stays ~30 KB.
    target_w = 384
    scale = target_w / rosa.width
    rosa = rosa.resize((target_w, int(rosa.height * scale)), Image.LANCZOS)
    cx = int(cx * scale)
    cy = int(cy * scale)
    buf = io.BytesIO()
    rosa.save(buf, "PNG", optimize=True)
    rosa_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    # SVG viewBox is 512×512. Mirror the PNG composition: same 10%
    # safe margin, same visual-centroid centering.
    margin = int(512 * 0.10)
    target_half = (512 - 2 * margin) // 2
    max_x_extent = max(cx, rosa.width - cx)
    max_y_extent = max(cy, rosa.height - cy)
    svg_scale = min(target_half / max_x_extent, target_half / max_y_extent)
    rose_w = int(rosa.width * svg_scale)
    rose_h = int(rosa.height * svg_scale)
    rose_x = 256 - int(cx * svg_scale)
    rose_y = 256 - int(cy * svg_scale)

    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" role="img" aria-label="Nima and Elien">
  <rect width="512" height="512" rx="96" ry="96" fill="#F5F0EB"/>
  <image x="{rose_x}" y="{rose_y}" width="{rose_w}" height="{rose_h}" preserveAspectRatio="xMidYMid meet" href="data:image/png;base64,{rosa_b64}"/>
</svg>
"""
    out_path.write_text(svg, encoding="utf-8")


def main():
    if not ROSA_PATH.exists():
        raise SystemExit(f"missing {ROSA_PATH}")
    ICONS_DIR.mkdir(exist_ok=True)

    targets = [
        ("icon-192.png", 192, dict(rounded=True)),
        ("icon-512.png", 512, dict(rounded=True)),
        ("apple-touch-icon.png", 180, dict(rounded=True)),
        # Maskable: full bleed (Android applies its own mask). The artwork
        # composition is identical to the rounded variants — same rose,
        # same placement, same size — so the icon reads consistently no
        # matter which platform/install path is picking it.
        ("icon-maskable-512.png", 512, dict(rounded=False)),
    ]
    for name, size, opts in targets:
        img = render_canvas(size, **opts)
        out = ICONS_DIR / name
        img.save(out, "PNG", optimize=True)
        print(f"  wrote icons/{name}  ({out.stat().st_size:,} bytes)")

    write_svg(ICONS_DIR / "icon.svg")
    print(f"  wrote icons/icon.svg")


if __name__ == "__main__":
    main()
