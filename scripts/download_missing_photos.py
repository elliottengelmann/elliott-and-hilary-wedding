#!/usr/bin/env python3
"""
Download guest photos from form_responses.csv that aren't yet present in
images/guests/. Uses FORM_NAME_MAP from merge_guests.py to map form-submitted
names to canonical filenames. Picks the most recent submission when a guest
has filled the form multiple times.

Extension is chosen from the downloaded file's magic bytes (jpg/png/heif),
matching how existing files in images/guests/ are named.
"""
import csv
import os
import re
import sys
import tempfile
from datetime import datetime

import gdown

DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, DIR)
from merge_guests import FORM_NAME_MAP, normalize, _first_matching_column  # noqa: E402

FORM_CSV = os.path.join(DIR, "form_responses.csv")
GUESTS_DIR = os.path.join(DIR, "images", "guests")

DRIVE_ID_RE = re.compile(r"[?&]id=([A-Za-z0-9_-]+)")


def drive_id(url: str):
    m = DRIVE_ID_RE.search(url or "")
    return m.group(1) if m else None


def canonical_key(first, last):
    key = (normalize(first), normalize(last))
    return FORM_NAME_MAP.get(key, key)


def stem_for(first, last):
    return f"{first}_{last}".lower().replace(" ", "_")


def existing_stems():
    if not os.path.isdir(GUESTS_DIR):
        return set()
    out = set()
    for fn in os.listdir(GUESTS_DIR):
        stem, _ = os.path.splitext(fn)
        out.add(stem)
    return out


def detect_ext(path: str) -> str:
    with open(path, "rb") as f:
        head = f.read(32)
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    # HEIF / HEIC: "....ftypheic" / "ftypheix" / "ftypmif1" / "ftyphevc" etc
    if len(head) >= 12 and head[4:8] == b"ftyp" and head[8:12] in (
        b"heic", b"heix", b"mif1", b"msf1", b"heis", b"hevc", b"hevx",
    ):
        return ".heif"
    if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
        return ".gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    return ""


def parse_ts(ts: str):
    ts = (ts or "").strip()
    try:
        return datetime.strptime(ts, "%m/%d/%Y %H:%M:%S")
    except ValueError:
        return datetime.min


def most_recent_submissions():
    """Return {canonical_key: (first_orig, last_orig, photo_url, ts)}
    picking the most recent row when a guest submitted multiple times."""
    latest = {}
    with open(FORM_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            first = (row.get("First Name") or "").strip()
            last = (row.get("Last Name") or "").strip()
            if not (first or last):
                continue
            ts = parse_ts(row.get("Timestamp", ""))
            photo_url = _first_matching_column(row, "Please Upload a Photo", "Photo URL")
            key = canonical_key(first, last)
            prev = latest.get(key)
            if prev is None or ts > prev[3]:
                latest[key] = (first, last, photo_url, ts)
    return latest


def main():
    have = existing_stems()
    latest = most_recent_submissions()

    missing = []
    for key, (first, last, url, ts) in sorted(latest.items()):
        canon_first, canon_last = key
        stem = stem_for(canon_first, canon_last)
        if stem in have:
            continue
        if not url:
            print(f"  skip {stem}: no photo URL in latest submission")
            continue
        fid = drive_id(url)
        if not fid:
            print(f"  skip {stem}: can't extract Drive id from {url!r}")
            continue
        missing.append((stem, fid, first, last))

    print(f"Downloading {len(missing)} missing photos...")
    ok, failed = [], []
    for stem, fid, first, last in missing:
        print(f"  {stem} (id={fid})")
        try:
            with tempfile.NamedTemporaryFile(delete=False) as tf:
                tmp_path = tf.name
            gdown.download(id=fid, output=tmp_path, quiet=True)
            ext = detect_ext(tmp_path)
            if not ext:
                print(f"    ! unknown file type, leaving as .bin for inspection")
                ext = ".bin"
            dest = os.path.join(GUESTS_DIR, f"{stem}{ext}")
            os.replace(tmp_path, dest)
            ok.append(dest)
        except Exception as e:
            print(f"    ! download failed: {e}")
            failed.append((stem, fid, str(e)))
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    print()
    print(f"Done: {len(ok)} saved, {len(failed)} failed")
    if failed:
        for stem, fid, err in failed:
            print(f"  FAILED {stem} ({fid}): {err}")


if __name__ == "__main__":
    main()
