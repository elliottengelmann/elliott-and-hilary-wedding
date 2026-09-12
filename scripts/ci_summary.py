#!/usr/bin/env python3
"""Print a Markdown summary of a sync run for the Actions job page.

Nobody is watching the automated pipeline, so this is where anything that
wants a human eye has to surface: guests whose photo never arrived, and
thumbnails that fell back to a centre crop because no face was found.
Writes to stdout; the workflow redirects it into $GITHUB_STEP_SUMMARY.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "wedding.db"
DERIVED = ROOT / "images" / "guests" / "derived"
# Deliberately not _no_face.txt: that file records the last run that *had* a
# fallback and isn't rewritten when a later run fixes one, so it goes stale
# and names guests who now have a pinned crop. The cache plus the overrides
# describe the current state exactly — a key is centre-cropped right now iff
# detection found no face and nobody has pinned a crop for it.
FACE_CACHE = DERIVED / "_face_cache.json"
OVERRIDES = DERIVED / "_overrides.json"


def centre_cropped_keys() -> list[str]:
    """Keys whose thumbnail is a blind centre crop as of this run."""
    try:
        cache = json.loads(FACE_CACHE.read_text())
    except (OSError, ValueError):
        return []
    try:
        overrides = json.loads(OVERRIDES.read_text())
    except (OSError, ValueError):
        overrides = {}
    return sorted(
        key for key, entry in cache.items()
        if entry.get("face") is None and key not in overrides
    )


def main() -> None:
    out: list[str] = ["### Guest sync", ""]

    if not DB.exists():
        out.append("`wedding.db` is missing — the sync did not get that far.")
        print("\n".join(out))
        return

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = list(conn.execute("SELECT first_name, last_name, photo_url FROM guests"))

    def name(r: sqlite3.Row) -> str:
        return f"{(r['first_name'] or '').strip()} {(r['last_name'] or '').strip()}".strip()

    local = [r for r in rows if r["photo_url"] and not r["photo_url"].startswith("http")]
    remote = [r for r in rows if (r["photo_url"] or "").startswith("http")]
    none_ = [r for r in rows if not r["photo_url"]]

    out.append(f"**{len(rows)}** guests · **{len(local)}** with a photo")
    out.append("")

    if remote:
        # The Apps Script should have committed the file alongside the CSV.
        # Still a raw Drive link means that upload failed — the guest is live
        # with an initials avatar and the photo needs fetching by hand.
        out.append(f"> [!WARNING]")
        out.append(f"> **{len(remote)} photo(s) never arrived** and are still raw "
                   f"Drive links. These guests show initials:")
        for r in remote:
            out.append(f"> - {name(r)}")
        out.append("")

    if none_:
        out.append(f"Submitted no photo (expected, shows initials): "
                   f"{', '.join(name(r) for r in none_)}")
        out.append("")

    keys = centre_cropped_keys()
    if keys:
        out.append("No face detected — centre-cropped, may be framed wrong:")
        out.extend(f"- `{k}`" for k in keys)
        out.append("")
        out.append("Fix one with:")
        out.append("")
        out.append("```bash")
        out.append("python3 scripts/process_guest_images.py --set-crop KEY "
                   "--cx N --cy N --size N")
        out.append("```")

    print("\n".join(out))


if __name__ == "__main__":
    main()
