#!/usr/bin/env python3
"""Decode an oversized Drive download_file_content tool result into a photo.

When a Drive download is too large to return inline, the harness saves the
raw JSON ({content, id, mimeType, title}) to a file and hands back its path.
This turns that file into images/guests/<stem>.<ext>.

Usage: save_drive_result.py <tool_result_json> <stem>
"""
import base64
import json
import os
import sys

DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUESTS_DIR = os.path.join(DIR, "images", "guests")

EXTS = [
    (b"\xff\xd8\xff", ".jpeg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
]


def detect_ext(head):
    for magic, ext in EXTS:
        if head.startswith(magic):
            return ext
    if len(head) >= 12 and head[4:8] == b"ftyp":
        if head[8:12] in (b"heic", b"heix", b"mif1", b"msf1", b"heis"):
            return ".heif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    return ""


def main():
    data = json.load(open(sys.argv[1]))
    stem = sys.argv[2]
    raw = base64.b64decode(data["content"])
    ext = detect_ext(raw[:32])
    if not ext:
        sys.exit(f"unknown image type for {stem}, head={raw[:12].hex()}")
    dest = os.path.join(GUESTS_DIR, stem + ext)
    with open(dest, "wb") as f:
        f.write(raw)
    print(f"wrote {os.path.relpath(dest, DIR)} ({len(raw):,} bytes) <- {data['title']}")


if __name__ == "__main__":
    main()
