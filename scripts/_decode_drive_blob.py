#!/usr/bin/env python3
"""Decode a Drive MCP download_file_content tool-result JSON into an image.

Usage: _decode_drive_blob.py <tool_result_json> <stem>

Writes images/guests/<stem>.<ext> where ext is detected from magic bytes.
"""
import base64
import json
import os
import sys

DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUESTS_DIR = os.path.join(DIR, "images", "guests")


def detect_ext(head: bytes) -> str:
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if len(head) >= 12 and head[4:8] == b"ftyp" and head[8:12] in (
        b"heic", b"heix", b"mif1", b"msf1", b"heis", b"hevc", b"hevx",
    ):
        return ".heif"
    if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
        return ".gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    return ""


def main():
    tool_result = sys.argv[1]
    stem = sys.argv[2]

    with open(tool_result, "r") as f:
        data = json.load(f)

    blob_b64 = data["content"][0]["embeddedResource"]["contents"]["blob"]
    raw = base64.b64decode(blob_b64)
    ext = detect_ext(raw[:32])
    if not ext:
        sys.stderr.write(f"unknown file type, head={raw[:16].hex()}\n")
        ext = ".bin"
    dest = os.path.join(GUESTS_DIR, f"{stem}{ext}")
    with open(dest, "wb") as f:
        f.write(raw)
    print(f"wrote {dest} ({len(raw)} bytes)")


if __name__ == "__main__":
    main()
