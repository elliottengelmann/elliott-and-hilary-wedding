#!/usr/bin/env python3
"""Convert guest photos that browsers can't display into JPEG.

process_guest_images.py only picks up .jpg/.jpeg/.png sources, and browsers
render none of the other formats phones produce — an iPhone uploading straight
from the photo library commonly sends HEIC/HEIF. Left alone such a file is
silently skipped: no thumbnail is generated, the profile falls back to the
initials avatar, and nothing warns you the guest actually sent a photo.

This converts every source in images/guests/ that isn't already a supported
format, replacing it in place (same stem, .jpeg extension) so resolve_photo()
in merge_guests.py picks it up unchanged. Originals are removed once the
converted file is written — the Drive copy remains the real original.

Run before process_guest_images.py:

    python3 scripts/normalize_photos.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageOps

try:  # registers HEIF/AVIF openers with Pillow
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:  # pragma: no cover - normalizer degrades, doesn't crash
    pillow_heif = None

ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT / "images" / "guests"

# What process_guest_images.py globs for, and what browsers render.
KEEP = {".jpg", ".jpeg", ".png"}
JPEG_QUALITY = 92


def convert(path: Path) -> Path | None:
    """Rewrite one source as JPEG. Returns the new path, or None on failure."""
    try:
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            dest = path.with_suffix(".jpeg")
            if dest.exists() and dest != path:
                print(f"  !! {path.name}: {dest.name} already exists, leaving both")
                return None
            im.save(dest, "JPEG", quality=JPEG_QUALITY)
    except Exception as exc:  # unreadable or unsupported codec
        print(f"  !! {path.name}: cannot convert ({exc.__class__.__name__}: {exc})")
        return None
    path.unlink()
    return dest


def main() -> int:
    if not SRC_DIR.is_dir():
        print(f"no {SRC_DIR.relative_to(ROOT)}, nothing to do")
        return 0

    stragglers = sorted(
        p for p in SRC_DIR.iterdir() if p.is_file() and p.suffix.lower() not in KEEP
    )
    if not stragglers:
        print("all guest photos are already jpg/jpeg/png")
        return 0

    print(f"converting {len(stragglers)} photo(s) to JPEG")
    failed = 0
    for path in stragglers:
        if pillow_heif is None and path.suffix.lower() in (".heic", ".heif"):
            print(f"  !! {path.name}: needs pillow-heif (pip install pillow-heif)")
            failed += 1
            continue
        dest = convert(path)
        if dest is None:
            failed += 1
        else:
            size = dest.stat().st_size
            print(f"  {path.name} -> {dest.name} ({size:,} bytes)")

    if failed:
        # Loud on purpose: a skipped photo means a guest shows initials.
        print(f"\n{failed} photo(s) could not be converted — those guests will "
              f"fall back to the initials avatar.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
