#!/usr/bin/env python3
"""Generate thumbnail + full-size derivatives for guest photos.

For each `images/guests/<key>.{jpg,png}` source, this writes three files
under `images/guests/derived/`:

  <key>-thumb.webp   200x200 square, face-centered crop, WebP q70
  <key>-thumb.jpg    same crop, JPEG q75 (fallback for old browsers)
  <key>-full.webp    long edge <= 1080px, original framing, WebP q80

Source files are never touched. Derivatives only regenerate when the
source mtime is newer than the derivative (or when the manual override
for that key has changed). Face detection is cached by source sha256
in `_face_cache.json` so re-runs skip the slow step.

Manual overrides live in `images/guests/derived/_overrides.json`:

    { "betty_royster": { "cx": 1820, "cy": 1100, "size": 2400 } }

cx/cy are pixel coordinates of the crop center in the original image;
size is the side length of the square crop (in original-image pixels).
An override always wins over face detection.

Run:
    python3 scripts/process_guest_images.py            # process all
    python3 scripts/process_guest_images.py --force    # ignore mtime cache
    python3 scripts/process_guest_images.py --only KEY # one guest
    python3 scripts/process_guest_images.py --set-crop KEY --cx N --cy N --size N
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT / "images" / "guests"
OUT_DIR = SRC_DIR / "derived"
CACHE_PATH = OUT_DIR / "_face_cache.json"
OVERRIDES_PATH = OUT_DIR / "_overrides.json"
NO_FACE_LOG = OUT_DIR / "_no_face.txt"

THUMB_SIZE = 200          # output square edge for grid avatars
FULL_MAX_EDGE = 1080      # output long edge for profile view
DETECT_LONG_EDGE = 1024   # downscaled long edge fed to HOG detector
FACE_PADDING = 0.8        # expand face bbox by this fraction on each side


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_json(path: Path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")


def detect_face_bbox(img: Image.Image) -> tuple[int, int, int, int] | None:
    """Return (top, right, bottom, left) of the largest face in the
    full-resolution image, or None if no face is detected. Detection
    runs on a downscaled copy for speed; coordinates are scaled back."""
    # Imported lazily so --set-crop runs without it, and so an environment
    # that couldn't install dlib (the CI runner, say) degrades to the
    # fallback centre crop instead of taking the whole sync down with it.
    try:
        import face_recognition
    except ImportError:
        print("  !! face_recognition unavailable — falling back to centre "
              "crops. Crops from this run need review.")
        return None
    import numpy as np

    w, h = img.size
    long_edge = max(w, h)
    scale = min(1.0, DETECT_LONG_EDGE / long_edge)
    if scale < 1.0:
        small = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    else:
        small = img
    arr = np.asarray(small.convert("RGB"))
    locations = face_recognition.face_locations(arr, model="hog")
    if not locations:
        return None
    # Pick largest face by area; tiebreak by proximity to image center.
    sw, sh = small.size
    cx_img, cy_img = sw / 2, sh / 2

    def score(loc):
        top, right, bottom, left = loc
        area = (bottom - top) * (right - left)
        cx = (left + right) / 2
        cy = (top + bottom) / 2
        center_dist = ((cx - cx_img) ** 2 + (cy - cy_img) ** 2) ** 0.5
        return (area, -center_dist)

    locations.sort(key=score, reverse=True)
    top, right, bottom, left = locations[0]
    inv = 1.0 / scale
    return (
        int(top * inv),
        int(right * inv),
        int(bottom * inv),
        int(left * inv),
    )


def crop_box_from_face(img_size: tuple[int, int],
                       bbox: tuple[int, int, int, int]) -> tuple[int, int, int]:
    """Given image dimensions and a face bbox, return (cx, cy, size)
    for a padded square crop. Size is clamped so the crop never
    overflows the image; if it would, the center is slid inward."""
    w, h = img_size
    top, right, bottom, left = bbox
    face_w = right - left
    face_h = bottom - top
    cx = (left + right) / 2
    cy = (top + bottom) / 2
    size = max(face_w, face_h) * (1.0 + 2.0 * FACE_PADDING)
    size = min(size, w, h)
    half = size / 2
    cx = max(half, min(w - half, cx))
    cy = max(half, min(h - half, cy))
    return int(round(cx)), int(round(cy)), int(round(size))


def fallback_crop_box(img_size: tuple[int, int]) -> tuple[int, int, int]:
    """Top-biased center crop for photos with no detected face: square
    side = shorter image edge, vertical center anchored at 40% of image
    height (clamped so the crop fits)."""
    w, h = img_size
    size = min(w, h)
    cx = w // 2
    half = size // 2
    cy = max(half, min(h - half, int(h * 0.4)))
    return cx, cy, size


def square_crop(img: Image.Image, cx: int, cy: int, size: int) -> Image.Image:
    half = size // 2
    left = cx - half
    top = cy - half
    return img.crop((left, top, left + size, top + size))


def write_thumb(crop: Image.Image, key: str) -> tuple[int, int]:
    """Write WebP + JPEG thumbnails for the grid. Returns (webp_bytes, jpg_bytes)."""
    thumb = crop.resize((THUMB_SIZE, THUMB_SIZE), Image.LANCZOS).convert("RGB")
    webp_path = OUT_DIR / f"{key}-thumb.webp"
    jpg_path = OUT_DIR / f"{key}-thumb.jpg"
    thumb.save(webp_path, "WEBP", quality=70, method=6)
    thumb.save(jpg_path, "JPEG", quality=75, optimize=True, progressive=True)
    return webp_path.stat().st_size, jpg_path.stat().st_size


def write_full(img: Image.Image, key: str) -> int:
    """Write the resized full-image WebP (no crop). Returns bytes written."""
    w, h = img.size
    long_edge = max(w, h)
    if long_edge > FULL_MAX_EDGE:
        scale = FULL_MAX_EDGE / long_edge
        new = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    else:
        new = img.copy()
    new = new.convert("RGB")
    full_path = OUT_DIR / f"{key}-full.webp"
    new.save(full_path, "WEBP", quality=80, method=6)
    return full_path.stat().st_size


def derivatives_for(key: str) -> list[Path]:
    return [
        OUT_DIR / f"{key}-thumb.webp",
        OUT_DIR / f"{key}-thumb.jpg",
        OUT_DIR / f"{key}-full.webp",
    ]


def process_one(src: Path, force: bool, cache: dict, overrides: dict,
                no_face: list[str]) -> tuple[bool, int]:
    """Returns (regenerated, total_bytes_written)."""
    key = src.stem
    derivs = derivatives_for(key)

    override = overrides.get(key)
    override_sig = json.dumps(override, sort_keys=True) if override else ""

    cache_entry = cache.get(key) or {}
    digest = sha256_of(src)
    sha_matches = cache_entry.get("sha256") == digest
    fresh = (
        not force
        and all(d.exists() for d in derivs)
        and sha_matches
        and cache_entry.get("override_sig", "") == override_sig
    )
    if fresh:
        return False, 0

    img = Image.open(src)
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")

    if override:
        cx = int(override["cx"])
        cy = int(override["cy"])
        size = int(override["size"])
        face_used = "override"
        face = cache_entry.get("face") if sha_matches else None
    else:
        # Reuse cached face bbox when source content is unchanged. This
        # is the slow path — face_recognition + dlib — so a SHA match
        # lets a fresh worktree skip detection entirely.
        face = cache_entry.get("face") if sha_matches else None
        if face is None and not sha_matches:
            bbox = detect_face_bbox(img)
            face = list(bbox) if bbox else None
        if face is None:
            cx, cy, size = fallback_crop_box(img.size)
            face_used = "fallback"
            no_face.append(key)
        else:
            cx, cy, size = crop_box_from_face(img.size, tuple(face))
            face_used = "face"

    cache[key] = {
        "sha256": digest,
        "face": face,
        "override_sig": override_sig,
    }

    crop = square_crop(img, cx, cy, size)
    thumb_webp, thumb_jpg = write_thumb(crop, key)
    full_webp = write_full(img, key)
    total = thumb_webp + thumb_jpg + full_webp
    print(f"  {key:<32s} {face_used:<9s} thumb {thumb_webp//1024}K/{thumb_jpg//1024}K  full {full_webp//1024}K")
    return True, total


def cmd_set_crop(args) -> int:
    overrides = load_json(OVERRIDES_PATH, {})
    overrides[args.set_crop] = {"cx": args.cx, "cy": args.cy, "size": args.size}
    save_json(OVERRIDES_PATH, overrides)
    print(f"override saved for {args.set_crop}: cx={args.cx} cy={args.cy} size={args.size}")
    print("re-run without --set-crop to regenerate derivatives.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate guest image derivatives.")
    parser.add_argument("--force", action="store_true",
                        help="Ignore mtime cache and regenerate everything.")
    parser.add_argument("--only", help="Process only this guest key.")
    parser.add_argument("--set-crop", metavar="KEY",
                        help="Pin a manual crop for KEY (also requires --cx --cy --size).")
    parser.add_argument("--cx", type=int)
    parser.add_argument("--cy", type=int)
    parser.add_argument("--size", type=int)
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    if args.set_crop:
        if args.cx is None or args.cy is None or args.size is None:
            parser.error("--set-crop requires --cx, --cy, and --size")
        return cmd_set_crop(args)

    cache = load_json(CACHE_PATH, {})
    overrides = load_json(OVERRIDES_PATH, {})

    sources = sorted(p for p in SRC_DIR.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png"))
    if args.only:
        sources = [p for p in sources if p.stem == args.only]
        if not sources:
            print(f"no source matches --only {args.only}", file=sys.stderr)
            return 1

    no_face: list[str] = []
    regenerated = 0
    skipped = 0
    total_bytes = 0
    print(f"processing {len(sources)} source images → {OUT_DIR.relative_to(ROOT)}/")
    for src in sources:
        try:
            did, n = process_one(src, args.force, cache, overrides, no_face)
        except Exception as e:
            print(f"  {src.stem:<32s} ERROR: {e}", file=sys.stderr)
            continue
        if did:
            regenerated += 1
            total_bytes += n
        else:
            skipped += 1

    # mtime is per-checkout, so stripping it keeps the committed
    # cache from churning every time a worktree is rebuilt.
    for entry in cache.values():
        entry.pop("src_mtime", None)
    save_json(CACHE_PATH, cache)
    # _no_face.txt is derived from the cumulative cache, not this run's
    # accumulator — that way --only and re-runs with a hot cache leave a
    # complete log instead of clobbering it. Override-pinned guests
    # don't appear in the log even if face detection originally missed.
    if not args.only:
        no_face_cumulative = sorted(
            k for k, v in cache.items()
            if v.get("face") is None and not (v.get("override_sig") or "")
        )
        if no_face_cumulative:
            NO_FACE_LOG.write_text("\n".join(no_face_cumulative) + "\n", encoding="utf-8")
        elif NO_FACE_LOG.exists():
            NO_FACE_LOG.unlink()

    print()
    print(f"done. regenerated {regenerated}, skipped {skipped} (cached).")
    if regenerated:
        print(f"wrote ~{total_bytes // 1024} KB total to {OUT_DIR.relative_to(ROOT)}/")
    if no_face:
        print(f"⚠ {len(no_face)} photo(s) had no detected face — see {NO_FACE_LOG.relative_to(ROOT)}")
        print("  pin a crop with: python3 scripts/process_guest_images.py --set-crop <key> --cx N --cy N --size N")
    return 0


if __name__ == "__main__":
    sys.exit(main())
