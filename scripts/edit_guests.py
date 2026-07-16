#!/usr/bin/env python3
"""
Local editor for the curation tables in wedding.db
(guest_locations, guest_memories, relationships).

Usage:
    python3 scripts/edit_guests.py

Opens http://localhost:8765/editor in your browser. Reads the attending guest
list from wedding.db and writes your edits straight back to it. Close
the tab and Ctrl+C the terminal to stop.

After you're done, run `python3 build.py` to regenerate index.html,
then commit the updated wedding.db (and index.html) and push.

IMPORTANT — NO FABRICATED CONTENT
Every value entered through this tool (memories, hometowns, cities,
relationships, labels) must be human-authored by Elien or Nima. Do
not paste in AI-generated text, do not guess at facts you don't
know, do not use placeholder content "to see how the design looks."
See CLAUDE.md for the full rule.
"""
from __future__ import annotations

import base64
import http.server
import json
import mimetypes
import os
import re
import sqlite3
import subprocess
import sys
import threading
import webbrowser
from typing import Any

DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(DIR, "wedding.db")
PHOTO_OVERRIDE_DIR = os.path.join(DIR, "images", "guests", "overrides")
GUIDE_PHOTO_DIR = os.path.join(DIR, "images", "guide")
PORT = 8765
MAX_PHOTO_BYTES = 20 * 1024 * 1024  # 20 MB
# Anything Pillow-less we can serve without conversion. Keep the list
# short and image-only — we don't want arbitrary file uploads.
ALLOWED_PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif"}
# Guide photos (currently Shopping only). Nima's convention from
# process_guest_images.py: Pillow pipeline, EXIF-rotated, WebP primary
# with JPEG fallback. These are storefront / product shots — wide-ish
# 3:2 center crop, max 1080×720, sized to fill a place-card body
# without breaking the page rhythm.
GUIDE_PHOTO_RATIO = (3, 2)
GUIDE_PHOTO_MAX_W = 1080
GUIDE_PHOTO_MAX_H = 720
GUIDE_PHOTO_WEBP_Q = 80
GUIDE_PHOTO_JPG_Q = 85

# Every relationship + memory row must carry a source from this set.
# Enforced at save time by this tool, at build time by build.py, and
# at the schema level by CHECK constraints in wedding.db.
ALLOWED_SOURCES = {"elien", "nima"}

CURATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS guest_locations (
    guest_key     TEXT PRIMARY KEY,
    current_city  TEXT DEFAULT '',
    hometown      TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS guest_memories (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guest_key  TEXT NOT NULL,
    subject    TEXT NOT NULL CHECK (subject IN ('them','nima','elien','both')),
    text       TEXT NOT NULL,
    source     TEXT NOT NULL CHECK (source IN ('elien','nima')),
    UNIQUE (guest_key, subject)
);

CREATE TABLE IF NOT EXISTS relationships (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    guest_a_key   TEXT NOT NULL,
    guest_b_key   TEXT NOT NULL,
    label         TEXT NOT NULL DEFAULT '',
    source        TEXT NOT NULL CHECK (source IN ('elien','nima'))
);

CREATE TABLE IF NOT EXISTS guest_field_overrides (
    guest_key  TEXT NOT NULL,
    field      TEXT NOT NULL CHECK (field IN ('how_we_know','least_favorite','photo_url','rsvp_thursday','rsvp_friday','display_name')),
    value      TEXT NOT NULL,
    source     TEXT NOT NULL CHECK (source IN ('elien','nima')),
    PRIMARY KEY (guest_key, field)
);

-- Post-wedding contact info. Populated from contact_form_responses.csv
-- by merge_guests.merge_contacts() (rows tagged source='form') and from
-- this editor (source='elien' or 'nima'). When the form sync next runs,
-- rows tagged elien/nima are preserved untouched — that's the manual
-- override mechanism. To "release" a manual override and let the form
-- take over again, delete the row from the editor.
CREATE TABLE IF NOT EXISTS guest_contacts (
    guest_key    TEXT PRIMARY KEY,
    phone        TEXT DEFAULT '',
    email        TEXT DEFAULT '',
    instagram    TEXT DEFAULT '',
    linkedin     TEXT DEFAULT '',
    twitter      TEXT DEFAULT '',
    bluesky      TEXT DEFAULT '',
    soundcloud   TEXT DEFAULT '',
    source       TEXT NOT NULL CHECK(source IN ('form','elien','nima')),
    submitted_at TEXT DEFAULT '',
    updated_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_relationships_a ON relationships(guest_a_key);
CREATE INDEX IF NOT EXISTS ix_relationships_b ON relationships(guest_b_key);
CREATE INDEX IF NOT EXISTS ix_memories_guest ON guest_memories(guest_key);
"""

# Field names of guest_contacts that the editor can write. `source`,
# `submitted_at`, `updated_at` are managed by the server-side save
# logic, not exposed in the UI directly.
CONTACT_FIELDS = ("phone", "email", "instagram", "linkedin", "twitter", "bluesky", "soundcloud")

OVERRIDE_FIELDS = (
    "how_we_know",
    "least_favorite",
    "photo_url",
    "rsvp_thursday",
    "rsvp_friday",
    "display_name",
)


def ensure_schema():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(CURATION_SCHEMA)

    # Migration: rebuild guest_field_overrides whenever the CHECK constraint
    # is missing a currently-valid override field. SQLite's CREATE TABLE IF
    # NOT EXISTS won't update CHECK constraints, so we have to detect the
    # drift and recreate the table from scratch.
    row = conn.execute(
        "SELECT sql FROM sqlite_schema WHERE type='table' AND name='guest_field_overrides'"
    ).fetchone()
    existing_sql = (row[0] or "") if row else ""
    if row and any(f not in existing_sql for f in OVERRIDE_FIELDS):
        conn.executescript("""
            CREATE TABLE guest_field_overrides_new (
                guest_key  TEXT NOT NULL,
                field      TEXT NOT NULL CHECK (field IN ('how_we_know','least_favorite','photo_url','rsvp_thursday','rsvp_friday','display_name')),
                value      TEXT NOT NULL,
                source     TEXT NOT NULL CHECK (source IN ('elien','nima')),
                PRIMARY KEY (guest_key, field)
            );
            INSERT INTO guest_field_overrides_new SELECT * FROM guest_field_overrides;
            DROP TABLE guest_field_overrides;
            ALTER TABLE guest_field_overrides_new RENAME TO guest_field_overrides;
        """)

    # Migration: add a `visible` flag to cafes and guide_places so the
    # admin can hide a listing from the rendered site without deleting
    # it from the DB. Default 1 (visible) so all existing rows stay
    # rendered exactly as before. Idempotent — only adds the column
    # when it's missing.
    for table in ("cafes", "guide_places"):
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if "visible" not in cols:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN visible INTEGER NOT NULL DEFAULT 1"
            )

    # Migration: add `has_rooftop` to guide_places (restaurants and any
    # other place that can carry the rooftop tag). Cafes intentionally
    # do NOT get this flag — per Elien, rooftop is a restaurant concept.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(guide_places)").fetchall()}
    if "has_rooftop" not in cols:
        conn.execute(
            "ALTER TABLE guide_places ADD COLUMN has_rooftop INTEGER NOT NULL DEFAULT 0"
        )

    # Migration: photo_path on guide_places. Currently only the Shopping
    # CMS surfaces an upload control, but the column lives on the table
    # so any future category that wants imagery can opt in without
    # another migration. Stored as a repo-relative path stem (no
    # extension) — the rendered <picture> tag picks .webp first with
    # .jpg fallback.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(guide_places)").fetchall()}
    if "photo_path" not in cols:
        conn.execute(
            "ALTER TABLE guide_places ADD COLUMN photo_path TEXT NOT NULL DEFAULT ''"
        )

    conn.commit()
    conn.close()


# ── I/O ────────────────────────────────────────────────────────────────────

def load_guests() -> list[dict[str, Any]]:
    """
    Returns the guest list with both form-sourced values (photo_url,
    how_we_know, least_favorite) AND per-guest field overrides merged
    in. `formPhotoUrl`/`formStory`/`formLeastFavorite` hold the raw
    form values so the editor UI can show what the guest submitted,
    even when an override has replaced it on the rendered profile.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # Editor shows guests who are either (a) currently attending — so
    # Elien can pre-curate content that takes effect when they submit
    # the form — or (b) have already filled out the form, even if they
    # later declined or haven't RSVP'd. A guest who has an admin
    # story override is also visible (without that they'd disappear
    # from the editor once the override unlocks their profile).
    #
    # Guests who declined OR didn't respond AND never filled out the
    # form are hidden — they'd never render on the live app so there's
    # no useful edit to make.
    rows = conn.execute(
        "SELECT first_name, last_name, photo_url, initials, how_we_know, least_favorite, rsvp_status, rsvp_thursday, rsvp_friday "
        "FROM guests "
        "ORDER BY last_name, first_name"
    ).fetchall()
    override_story_keys = {
        r[0] for r in conn.execute(
            "SELECT guest_key FROM guest_field_overrides WHERE field='how_we_know'"
        )
    }
    def _is_visible(r, key):
        if (r["rsvp_status"] or "").strip() == "Attending":
            return True
        if (r["how_we_know"] or "").strip():
            return True
        if key in override_story_keys:
            return True
        return False
    # field-overrides keyed by guest_key
    overrides: dict[str, dict[str, str]] = {}
    for r in conn.execute(
        "SELECT guest_key, field, value FROM guest_field_overrides"
    ):
        overrides.setdefault(r["guest_key"], {})[r["field"]] = r["value"]
    conn.close()

    out = []
    for r in rows:
        first = (r["first_name"] or "").strip()
        last = (r["last_name"] or "").strip()
        key = f"{first.lower()}_{last.lower()}".replace(" ", "_")
        if not _is_visible(r, key):
            continue
        o = overrides.get(key, {})
        form_photo = r["photo_url"] or ""
        form_story = r["how_we_know"] or ""
        form_least = r["least_favorite"] or ""
        form_thu   = r["rsvp_thursday"] or ""
        form_fri   = r["rsvp_friday"] or ""
        csv_name = f"{first} {last}".strip()
        display_name = (o.get("display_name") or "").strip() or csv_name
        out.append({
            "key": key,
            "firstName": first,
            "lastName": last,
            "name": display_name,        # effective (override or CSV)
            "csvName": csv_name,         # original CSV name for revert
            "displayNameOverride": (o.get("display_name") or "").strip(),
            "initials": r["initials"] or "",
            # Effective values (override falls through to form)
            "photoUrl":      o.get("photo_url")     or form_photo,
            "story":         o.get("how_we_know")   or form_story,
            "leastFavorite": o.get("least_favorite") or form_least,
            "rsvpThursday":  o.get("rsvp_thursday") or form_thu,
            "rsvpFriday":    o.get("rsvp_friday")   or form_fri,
            # Raw form values so the UI can label source and let admin revert
            "formPhotoUrl":      form_photo,
            "formStory":         form_story,
            "formLeastFavorite": form_least,
            "formRsvpThursday":  form_thu,
            "formRsvpFriday":    form_fri,
            # Which fields are currently overridden
            "overrides": {k: v for k, v in o.items()},
        })
    return out


# Baseline of cities where Elien's guests are likely to live — so the
# datalist has useful suggestions even before other guests fill anything
# in. Merged at runtime with whatever cities already appear in the data.
_BASELINE_CITIES = (
    "New York",
    "Brooklyn",
    "Manhattan",
    "Queens",
    "San Francisco",
    "Oakland",
    "Berkeley",
    "Los Angeles",
    "San Diego",
    "San Miguel de Allende",
    "Mexico City",
    "Portland",
    "Seattle",
    "Chicago",
    "Boston",
    "Philadelphia",
    "Washington, DC",
    "Austin",
    "Nashville",
    "Miami",
    "Atlanta",
    "New Orleans",
    "Denver",
    "Minneapolis",
    "Toronto",
    "Montreal",
    "Vancouver",
    "London",
    "Paris",
    "Berlin",
    "Amsterdam",
    "Barcelona",
    "Madrid",
    "Lisbon",
    "Rome",
    "Milan",
    "Istanbul",
    "Tokyo",
    "Lamoine, Maine",
    "Ellsworth, Maine",
    "Portland, Maine",
    "Bar Harbor, Maine",
)


# Known typo / variant → canonical. Applied only to the autocomplete
# dropdown so Elien sees one tidy entry per place, not five variants.
# The underlying form data stays as each guest wrote it.
_CITY_CANONICAL = {
    "san fransisco":  "San Francisco",
    "new york":       "New York, New York",
    "new york city":  "New York, New York",
    "nyc":            "New York, New York",
    "new york, ny":   "New York, New York",
}


def _canonicalize_city(s: str) -> str:
    return _CITY_CANONICAL.get((s or "").strip().lower(), (s or "").strip())


def load_known_cities() -> list[str]:
    """
    Every distinct city string currently in the data — from Elien's
    curation tables and the guests' own form answers — plus a small
    baseline of likely cities. Used to populate the city autocomplete
    datalist in the editor. Variants like "nyc" / "new york city" /
    "san fransisco" get folded to their canonical form (_CITY_CANONICAL)
    so the dropdown stays clean.
    """
    cities: set[str] = {_canonicalize_city(c) for c in _BASELINE_CITIES}
    conn = sqlite3.connect(DB_PATH)
    try:
        for sql in (
            "SELECT current_city FROM guest_locations",
            "SELECT hometown FROM guest_locations",
            "SELECT form_current_city FROM guests",
            "SELECT form_hometown FROM guests",
        ):
            try:
                for (value,) in conn.execute(sql):
                    v = _canonicalize_city(value)
                    if v:
                        cities.add(v)
            except sqlite3.OperationalError:
                # Column may not exist yet (e.g. merge_guests.py hasn't
                # re-run since Nima added form_current_city). Skip.
                continue
    finally:
        conn.close()
    return sorted(cities, key=lambda s: s.lower())


def load_guide_places() -> list[dict[str, Any]]:
    """Read all rows from the guide_places table for the Guide CMS tab."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, category, name, note, address, directions_url, visible, has_rooftop, photo_path "
        "FROM guide_places ORDER BY category, id"
    ).fetchall()
    conn.close()
    return [
        {
            "category": r["category"] or "",
            "name": r["name"] or "",
            "note": r["note"] or "",
            "address": r["address"] or "",
            "directionsUrl": r["directions_url"] or "",
            "visible": bool(r["visible"]),
            "hasRooftop": bool(r["has_rooftop"]),
            "photoPath": r["photo_path"] or "",
        }
        for r in rows
    ]


def load_cafes() -> list[dict[str, Any]]:
    """Read all rows from the cafes table for the Coffee Map CMS tab."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, name, note, address, directions_url, visible FROM cafes ORDER BY id"
    ).fetchall()
    conn.close()
    return [
        {
            "name": r["name"] or "",
            "note": r["note"] or "",
            "address": r["address"] or "",
            "directionsUrl": r["directions_url"] or "",
            "visible": bool(r["visible"]),
        }
        for r in rows
    ]


def load_known_categories() -> list[str]:
    """Distinct category names — fed to the Guide CMS's category datalist."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT DISTINCT category FROM guide_places "
        "WHERE category IS NOT NULL AND category != '' "
        "ORDER BY category"
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def load_curation() -> dict[str, Any]:
    """Read every curation row from wedding.db and shape it for the UI."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    locations: dict[str, dict[str, str]] = {}
    for r in conn.execute("SELECT guest_key, current_city, hometown FROM guest_locations"):
        locations[r["guest_key"]] = {
            "currentCity": r["current_city"] or "",
            "hometown": r["hometown"] or "",
        }

    memories = [
        {"guest": r["guest_key"], "subject": r["subject"], "text": r["text"], "source": r["source"]}
        for r in conn.execute(
            "SELECT guest_key, subject, text, source FROM guest_memories ORDER BY guest_key, subject"
        )
    ]

    relationships = [
        {"a": r["guest_a_key"], "b": r["guest_b_key"], "label": r["label"] or "", "source": r["source"]}
        for r in conn.execute(
            "SELECT guest_a_key, guest_b_key, label, source FROM relationships ORDER BY id"
        )
    ]

    field_overrides: dict[str, dict[str, dict[str, str]]] = {}
    for r in conn.execute(
        "SELECT guest_key, field, value, source FROM guest_field_overrides"
    ):
        field_overrides.setdefault(r["guest_key"], {})[r["field"]] = {
            "value": r["value"], "source": r["source"]
        }

    contacts: dict[str, dict[str, str]] = {}
    for r in conn.execute(
        "SELECT guest_key, phone, email, instagram, linkedin, twitter, bluesky, "
        "soundcloud, source FROM guest_contacts"
    ):
        contacts[r["guest_key"]] = {
            "phone":      r["phone"] or "",
            "email":      r["email"] or "",
            "instagram":  r["instagram"] or "",
            "linkedin":   r["linkedin"] or "",
            "twitter":    r["twitter"] or "",
            "bluesky":    r["bluesky"] or "",
            "soundcloud": r["soundcloud"] or "",
            "source":     r["source"],
        }

    conn.close()
    return {
        "locations": locations,
        "memories": memories,
        "relationships": relationships,
        "fieldOverrides": field_overrides,
        "contacts": contacts,
    }


def save_curation(data: dict[str, Any]) -> None:
    """
    Replace-all semantics: wipe the three curation tables and re-insert
    from the payload. Runs in a single transaction — partial writes roll
    back on any validation error.
    """
    # Server-side validation of every row's source before we touch the DB.
    for i, m in enumerate(data.get("memories") or []):
        if m.get("source") not in ALLOWED_SOURCES:
            raise ValueError(
                f"memories[{i}] has invalid source {m.get('source')!r}; "
                f"must be one of {sorted(ALLOWED_SOURCES)!r}"
            )
    for i, r in enumerate(data.get("relationships") or []):
        if r.get("source") not in ALLOWED_SOURCES:
            raise ValueError(
                f"relationships[{i}] has invalid source {r.get('source')!r}; "
                f"must be one of {sorted(ALLOWED_SOURCES)!r}"
            )

    # Validate field overrides
    for guest_key, fields in (data.get("fieldOverrides") or {}).items():
        for field, row in fields.items():
            if field not in OVERRIDE_FIELDS:
                raise ValueError(f"fieldOverrides[{guest_key}]: unknown field {field!r}")
            src = (row or {}).get("source")
            if src not in ALLOWED_SOURCES:
                raise ValueError(
                    f"fieldOverrides[{guest_key}][{field}] has invalid source {src!r}; "
                    f"must be one of {sorted(ALLOWED_SOURCES)!r}"
                )

    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("BEGIN")
        conn.execute("DELETE FROM guest_locations")
        conn.execute("DELETE FROM guest_memories")
        conn.execute("DELETE FROM relationships")
        conn.execute("DELETE FROM guest_field_overrides")
        # Guide CMS: full replace-all of the guide_places table.
        # Auto-increment IDs are dropped each save — no other table
        # references guide_places.id so this is safe.
        conn.execute("DELETE FROM guide_places")
        # Coffee Map CMS: same pattern for the cafes table.
        conn.execute("DELETE FROM cafes")

        for key, loc in (data.get("locations") or {}).items():
            current_city = (loc.get("currentCity") or "").strip()
            hometown = (loc.get("hometown") or "").strip()
            if not current_city and not hometown:
                continue
            conn.execute(
                "INSERT INTO guest_locations (guest_key, current_city, hometown) VALUES (?, ?, ?)",
                (key, current_city, hometown),
            )

        for m in (data.get("memories") or []):
            text = (m.get("text") or "").strip()
            if not text:
                continue
            conn.execute(
                "INSERT INTO guest_memories (guest_key, subject, text, source) VALUES (?, ?, ?, ?)",
                (m["guest"], m["subject"], text, m["source"]),
            )

        for r in (data.get("relationships") or []):
            conn.execute(
                "INSERT INTO relationships (guest_a_key, guest_b_key, label, source) VALUES (?, ?, ?, ?)",
                (r["a"], r["b"], (r.get("label") or "").strip(), r["source"]),
            )

        for guest_key, fields in (data.get("fieldOverrides") or {}).items():
            for field, row in fields.items():
                value = (row.get("value") or "").strip()
                if not value:
                    # Empty override = revert to form value. Just drop the row.
                    continue
                conn.execute(
                    "INSERT INTO guest_field_overrides (guest_key, field, value, source) VALUES (?, ?, ?, ?)",
                    (guest_key, field, value, row["source"]),
                )

        # Contacts: replace-all semantics only over the keys present in
        # the payload. Untouched form-sourced rows aren't in the payload
        # and stay in place; rows the editor saw (touched or not) get
        # rewritten with whatever the UI ended up with — including
        # outright deletion if every field went empty.
        import datetime as _dt
        now_iso = _dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        for guest_key, contact in (data.get("contacts") or {}).items():
            src = (contact or {}).get("source")
            if src not in ALLOWED_SOURCES:
                # Editor should only ever send back rows it manages
                # (elien/nima). Defence-in-depth — silently skip
                # malformed rows rather than promoting a form row to
                # manual without an explicit edit.
                continue
            fields = {f: ((contact.get(f) or "").strip()) for f in CONTACT_FIELDS}
            # Always clear any existing row at this key, then re-insert
            # only if something is set. An all-empty save deletes the
            # manual row and lets the form's data (if any) flow back
            # in on the next merge_contacts run.
            conn.execute("DELETE FROM guest_contacts WHERE guest_key = ?", (guest_key,))
            if not any(fields.values()):
                continue
            conn.execute(
                "INSERT INTO guest_contacts ("
                "  guest_key, phone, email, instagram, linkedin, "
                "  twitter, bluesky, soundcloud, source, submitted_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?)",
                (
                    guest_key,
                    fields["phone"], fields["email"], fields["instagram"],
                    fields["linkedin"], fields["twitter"], fields["bluesky"],
                    fields["soundcloud"], src, now_iso,
                ),
            )

        for p in (data.get("guide") or []):
            name = (p.get("name") or "").strip()
            if not name:
                # Empty name = unsaved blank row. Skip — don't persist.
                continue
            conn.execute(
                "INSERT INTO guide_places "
                "(category, name, note, address, directions_url, visible, has_rooftop, photo_path) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    (p.get("category") or "").strip(),
                    name,
                    (p.get("note") or "").strip(),
                    (p.get("address") or "").strip(),
                    (p.get("directionsUrl") or "").strip(),
                    1 if p.get("visible", True) else 0,
                    1 if p.get("hasRooftop", False) else 0,
                    (p.get("photoPath") or "").strip(),
                ),
            )

        for c in (data.get("cafes") or []):
            name = (c.get("name") or "").strip()
            if not name:
                continue
            conn.execute(
                "INSERT INTO cafes (name, note, address, directions_url, visible) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    name,
                    (c.get("note") or "").strip(),
                    (c.get("address") or "").strip(),
                    (c.get("directionsUrl") or "").strip(),
                    1 if c.get("visible", True) else 0,
                ),
            )

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── HTTP handler ───────────────────────────────────────────────────────────

_GUEST_KEY_RE = re.compile(r"^[a-z0-9_-]{1,80}$")


def save_uploaded_photo(guest_key: str, filename: str, mimetype: str, data_b64: str) -> str:
    """
    Decode a base64 upload and save it to images/guests/overrides/{key}.{ext}.
    Returns the repo-relative path (what goes into the photo_url override).
    Raises ValueError on bad input.
    """
    if not _GUEST_KEY_RE.match(guest_key or ""):
        raise ValueError(f"invalid guestKey {guest_key!r}")

    # Pick an extension: prefer the uploaded filename's, fall back to mimetype.
    ext = os.path.splitext(filename or "")[1].lower()
    if ext not in ALLOWED_PHOTO_EXTS:
        guessed = mimetypes.guess_extension(mimetype or "") or ""
        ext = guessed.lower() if guessed.lower() in ALLOWED_PHOTO_EXTS else ""
    if not ext:
        raise ValueError(
            f"unsupported photo format (ext={os.path.splitext(filename or '')[1]!r}, "
            f"mimetype={mimetype!r}); expected one of {sorted(ALLOWED_PHOTO_EXTS)}"
        )

    try:
        data = base64.b64decode(data_b64 or "", validate=True)
    except Exception as e:
        raise ValueError(f"bad base64 payload: {e}")
    if len(data) > MAX_PHOTO_BYTES:
        raise ValueError(f"photo too large: {len(data)} bytes > {MAX_PHOTO_BYTES}")
    if len(data) == 0:
        raise ValueError("empty photo payload")

    os.makedirs(PHOTO_OVERRIDE_DIR, exist_ok=True)
    # Clean up any prior override for this guest regardless of extension so
    # we don't leave stale files behind on extension changes.
    for fn in os.listdir(PHOTO_OVERRIDE_DIR):
        stem, old_ext = os.path.splitext(fn)
        if stem == guest_key and old_ext.lower() in ALLOWED_PHOTO_EXTS:
            try:
                os.remove(os.path.join(PHOTO_OVERRIDE_DIR, fn))
            except OSError:
                pass

    out_name = f"{guest_key}{ext}"
    out_path = os.path.join(PHOTO_OVERRIDE_DIR, out_name)
    tmp_path = out_path + ".tmp"
    with open(tmp_path, "wb") as f:
        f.write(data)
    os.replace(tmp_path, out_path)
    return f"images/guests/overrides/{out_name}"


def save_guide_photo(filename: str, mimetype: str, data_b64: str,
                     replace_path: str = "") -> str:
    """
    Decode + center-crop + resize a Shopping CMS upload into a 3:2 WebP
    (primary) and JPEG (fallback) under images/guide/. Returns the
    repo-relative path stem (no extension) — the build picks .webp first
    via <picture><source>, falls back to .jpg for older browsers.

    `replace_path`, if provided, is a previously-returned path stem
    whose .webp/.jpg derivatives will be unlinked after the new files
    land. Best-effort cleanup; failures don't propagate (the new photo
    has already been written).
    """
    try:
        from PIL import Image, ImageOps
    except ImportError as e:
        raise ValueError(f"Pillow not available: {e}")

    ext = os.path.splitext(filename or "")[1].lower()
    if ext and ext not in ALLOWED_PHOTO_EXTS:
        # Be permissive: trust mimetype if the filename has a weird ext.
        guessed = mimetypes.guess_extension(mimetype or "") or ""
        if guessed.lower() not in ALLOWED_PHOTO_EXTS:
            raise ValueError(
                f"unsupported photo format (ext={ext!r}, mimetype={mimetype!r}); "
                f"expected one of {sorted(ALLOWED_PHOTO_EXTS)}"
            )

    try:
        data = base64.b64decode(data_b64 or "", validate=True)
    except Exception as e:
        raise ValueError(f"bad base64 payload: {e}")
    if len(data) > MAX_PHOTO_BYTES:
        raise ValueError(f"photo too large: {len(data)} bytes > {MAX_PHOTO_BYTES}")
    if len(data) == 0:
        raise ValueError("empty photo payload")

    os.makedirs(GUIDE_PHOTO_DIR, exist_ok=True)

    import io
    try:
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")
    except Exception as e:
        raise ValueError(f"could not read image: {e}")

    # Center-crop to GUIDE_PHOTO_RATIO (3:2 landscape). Trim the longer
    # axis so the kept region is centered — works for both portrait
    # and landscape sources.
    rw, rh = GUIDE_PHOTO_RATIO
    w, h = img.size
    target_ratio = rw / rh
    src_ratio = w / h
    if src_ratio > target_ratio:
        # Source is wider than target — trim left+right.
        new_w = int(round(h * target_ratio))
        left = (w - new_w) // 2
        img = img.crop((left, 0, left + new_w, h))
    elif src_ratio < target_ratio:
        # Source is taller — trim top+bottom (centered).
        new_h = int(round(w / target_ratio))
        top = (h - new_h) // 2
        img = img.crop((0, top, w, top + new_h))

    # Resize down to max edges. Never upscale.
    if img.width > GUIDE_PHOTO_MAX_W or img.height > GUIDE_PHOTO_MAX_H:
        img = img.resize((GUIDE_PHOTO_MAX_W, GUIDE_PHOTO_MAX_H), Image.LANCZOS)

    # Random key — short, URL-safe, low collision risk for our scale
    # (handful of shopping entries). Hyphen-stripped uuid is overkill;
    # 12 hex chars is plenty.
    import secrets
    key = secrets.token_hex(6)
    webp_path = os.path.join(GUIDE_PHOTO_DIR, f"{key}.webp")
    jpg_path = os.path.join(GUIDE_PHOTO_DIR, f"{key}.jpg")
    img.save(webp_path, "WEBP", quality=GUIDE_PHOTO_WEBP_Q, method=6)
    img.save(jpg_path, "JPEG", quality=GUIDE_PHOTO_JPG_Q, optimize=True, progressive=True)

    # Best-effort cleanup of the previous photo's derivatives. Validate
    # the path stays inside GUIDE_PHOTO_DIR so a malicious replacePath
    # can't delete arbitrary files.
    if replace_path:
        for ext in (".webp", ".jpg"):
            old_full = os.path.join(DIR, replace_path + ext)
            real_old = os.path.realpath(old_full)
            real_dir = os.path.realpath(GUIDE_PHOTO_DIR)
            if real_old.startswith(real_dir + os.sep) and os.path.isfile(real_old):
                try:
                    os.remove(real_old)
                except OSError:
                    pass

    return f"images/guide/{key}"


def rebuild_site() -> tuple[bool, str]:
    """
    Run build.py to regenerate index.html. Returns (ok, stderr_if_fail).
    The editor calls this after every successful save so the preview
    iframe reloads the fresh output automatically.
    """
    result = subprocess.run(
        [sys.executable, "build.py"],
        cwd=DIR,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return True, ""
    msg = (result.stderr or result.stdout or "build.py failed").strip()
    return False, msg


# SimpleHTTPRequestHandler serves static files from its `directory`. We
# override do_GET / do_POST for a few specific routes; anything else
# (index.html, images/, sw.js, manifest.webmanifest, etc.) falls through
# to the default static file serving so the live preview can render the
# app from the same origin as the editor.
class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIR, **kwargs)

    def do_GET(self):  # noqa: N802
        path_only = self.path.split("?", 1)[0]
        if path_only in ("/editor", "/editor/"):
            self._send_html(INDEX_HTML)
        elif path_only == "/api/data":
            self._send_json({
                "guests": load_guests(),
                "curation": load_curation(),
                "knownCities": load_known_cities(),
                "guide": load_guide_places(),
                "knownCategories": load_known_categories(),
                "cafes": load_cafes(),
            })
        else:
            super().do_GET()

    def do_POST(self):  # noqa: N802
        if self.path == "/api/photo":
            self._handle_photo_upload()
            return
        if self.path == "/api/guide_photo":
            self._handle_guide_photo_upload()
            return
        if self.path != "/api/save":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as e:
            self._send_json({"ok": False, "error": f"Bad JSON: {e}"}, status=400)
            return
        try:
            save_curation(payload)
        except ValueError as e:
            self._send_json({"ok": False, "error": str(e)}, status=400)
            return
        except Exception as e:  # pragma: no cover
            self._send_json({"ok": False, "error": str(e)}, status=500)
            return

        # Regenerate index.html so the preview iframe picks up changes
        # on its next reload. Any build error surfaces back to the UI.
        built, build_err = rebuild_site()
        if not built:
            self._send_json({"ok": False, "error": f"Saved, but build.py failed:\n{build_err}"}, status=500)
            return
        self._send_json({"ok": True, "rebuilt": True})

    def _handle_photo_upload(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_PHOTO_BYTES * 2:
            self._send_json({"ok": False, "error": "empty or oversize upload"}, status=400)
            return
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as e:
            self._send_json({"ok": False, "error": f"Bad JSON: {e}"}, status=400)
            return
        try:
            path = save_uploaded_photo(
                guest_key=payload.get("guestKey", ""),
                filename=payload.get("filename", ""),
                mimetype=payload.get("mimetype", ""),
                data_b64=payload.get("dataBase64", ""),
            )
        except ValueError as e:
            self._send_json({"ok": False, "error": str(e)}, status=400)
            return
        except Exception as e:  # pragma: no cover
            self._send_json({"ok": False, "error": str(e)}, status=500)
            return
        self._send_json({"ok": True, "path": path})

    def _handle_guide_photo_upload(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_PHOTO_BYTES * 2:
            self._send_json({"ok": False, "error": "empty or oversize upload"}, status=400)
            return
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as e:
            self._send_json({"ok": False, "error": f"Bad JSON: {e}"}, status=400)
            return
        try:
            path = save_guide_photo(
                filename=payload.get("filename", ""),
                mimetype=payload.get("mimetype", ""),
                data_b64=payload.get("dataBase64", ""),
                replace_path=payload.get("replacePath", ""),
            )
        except ValueError as e:
            self._send_json({"ok": False, "error": str(e)}, status=400)
            return
        except Exception as e:  # pragma: no cover
            self._send_json({"ok": False, "error": str(e)}, status=500)
            return
        self._send_json({"ok": True, "path": path})

    # Quiet the default stderr log line per request — noisy in the terminal.
    def log_message(self, format, *args):  # noqa: A002
        return

    # Helpers.
    def _send_html(self, body: str):
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, obj: Any, status: int = 200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


# ── UI (single-page app, inline) ───────────────────────────────────────────

INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Los Invitados Editor</title>
<link href="https://fonts.googleapis.com/css2?family=Mea+Culpa&family=Bodoni+Moda:ital@0;1&family=Nunito:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  :root {
    --primary-green: #4A7C59;
    --deep-green: #3A6B48;
    --cream: #F5F0EB;
    --dark: #2D2D2D;
    --mid-gray: #666;
    --divider: #E5DDD3;
    --danger: #b64242;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; background: var(--cream); color: var(--dark); font-family: 'Nunito', sans-serif; height: 100%; }

  /* Split view: editor on the left, live preview iframe on the right. */
  #app-shell { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 440px); height: 100vh; }
  #editor-column { overflow-y: auto; min-width: 0; }
  #preview-column { border-left: 1px solid var(--divider); background: white; display: flex; flex-direction: column; min-height: 0; }
  #preview-bar {
    padding: 8px 12px; border-bottom: 1px solid var(--divider);
    display: flex; align-items: center; gap: 10px;
    font-size: 12px; color: var(--mid-gray);
  }
  #preview-bar button {
    font: inherit; background: white; border: 1px solid var(--divider);
    border-radius: 4px; padding: 3px 10px; cursor: pointer; color: var(--dark);
  }
  #preview-bar button:hover { border-color: var(--primary-green); color: var(--primary-green); }
  #preview-bar a { color: var(--primary-green); text-decoration: none; margin-left: auto; }
  #preview-bar a:hover { text-decoration: underline; }
  #preview-frame { flex: 1; width: 100%; border: none; display: block; background: var(--cream); }

  @media (max-width: 900px) {
    #app-shell { grid-template-columns: 1fr; grid-template-rows: 1fr 50vh; height: auto; }
    #preview-column { border-left: none; border-top: 1px solid var(--divider); }
  }

  header {
    padding: 20px 32px 12px;
    display: flex; align-items: baseline; gap: 24px;
    border-bottom: 1px solid var(--divider);
    position: sticky; top: 0; background: var(--cream); z-index: 10;
  }
  header h1 {
    font-family: 'Mea Culpa', cursive; font-weight: 400; font-size: 32px;
    color: var(--primary-green); margin: 0;
  }
  nav { display: flex; gap: 4px; }
  nav button {
    font-family: 'Bodoni Moda', serif; font-size: 15px;
    background: none; border: none; cursor: pointer;
    color: var(--mid-gray); padding: 6px 14px; border-radius: 6px;
  }
  nav button.active { color: var(--primary-green); background: white; border: 1px solid var(--divider); }
  .signed-in { margin-left: auto; display: flex; align-items: center; gap: 10px; font-size: 13px; color: var(--mid-gray); }
  .signed-in select { font: inherit; color: var(--dark); background: white; border: 1px solid var(--divider); border-radius: 4px; padding: 4px 8px; }
  #save-btn {
    font: inherit; font-weight: 600;
    background: var(--primary-green); color: white;
    border: none; border-radius: 4px; padding: 6px 16px; cursor: pointer;
    transition: background 0.15s ease, opacity 0.15s ease;
  }
  #save-btn:hover:not([disabled]) { background: var(--deep-green); }
  #save-btn[disabled] { background: var(--divider); color: var(--mid-gray); cursor: default; }
  #status { font-size: 13px; font-style: italic; color: var(--mid-gray); min-width: 9ch; text-align: left; }
  #status.dirty { color: var(--danger); font-style: normal; font-weight: 600; }
  #status.saved { color: var(--primary-green); }
  main { padding: 20px 32px 48px; max-width: 1100px; margin: 0 auto; }

  /* People tab */
  .people-wrap { display: grid; grid-template-columns: 280px 1fr; gap: 24px; min-height: 70vh; }
  .people-list { border: 1px solid var(--divider); border-radius: 8px; background: white; overflow: hidden; display: flex; flex-direction: column; }
  .people-search { padding: 10px; border-bottom: 1px solid var(--divider); }
  .people-search input { width: 100%; padding: 8px 10px; border: 1px solid var(--divider); border-radius: 4px; font: inherit; }
  .people-items { overflow-y: auto; max-height: 70vh; }
  .person-row { display: flex; align-items: center; gap: 10px; padding: 8px 12px; cursor: pointer; border-bottom: 1px solid var(--divider); }
  .person-row:last-child { border-bottom: none; }
  .person-row:hover { background: rgba(74,124,89,0.05); }
  .person-row.selected { background: rgba(74,124,89,0.1); }
  .person-thumb { width: 32px; height: 32px; border-radius: 50%; overflow: hidden; display: flex; align-items: center; justify-content: center; background: var(--divider); color: white; font-size: 11px; font-weight: 600; flex-shrink: 0; }
  .person-thumb img { width: 100%; height: 100%; object-fit: cover; }
  .person-name { font-family: 'Bodoni Moda', serif; font-size: 14px; }
  .person-marks { margin-left: auto; display: flex; gap: 3px; font-size: 10px; color: var(--primary-green); }
  .person-marks span { border: 1px solid var(--primary-green); border-radius: 3px; padding: 1px 4px; line-height: 1; }

  .editor { border: 1px solid var(--divider); border-radius: 8px; background: white; padding: 24px 28px; }
  .editor h2 { font-family: 'Bodoni Moda', serif; font-size: 20px; font-weight: 600; margin: 0 0 4px; }
  .editor .photo { width: 80px; height: 80px; border-radius: 8px; overflow: hidden; background: var(--divider); margin-bottom: 14px; }
  .editor .photo img { width: 100%; height: 100%; object-fit: cover; }
  .field { margin: 14px 0; }
  .field label { display: block; font-size: 12px; color: var(--mid-gray); text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 4px; }
  .field input, .field textarea {
    width: 100%; font-family: 'Nunito', sans-serif; font-size: 14px;
    border: 1px solid var(--divider); border-radius: 4px; padding: 8px 10px; background: #fafafa;
  }
  .field textarea { min-height: 70px; resize: vertical; line-height: 1.5; }
  .field input:focus, .field textarea:focus { outline: none; border-color: var(--primary-green); background: white; }
  .editor .empty { color: var(--mid-gray); font-style: italic; padding: 40px 0; text-align: center; }

  .memory-group { margin: 14px 0; padding: 14px; background: #fafafa; border-radius: 6px; border: 1px solid var(--divider); }
  .memory-group h3 { margin: 0 0 6px; font-family: 'Bodoni Moda', serif; font-weight: 600; font-size: 14px; color: var(--primary-green); }
  .field-note { font-size: 12px; color: var(--mid-gray); font-style: italic; display: block; margin-bottom: 8px; }
  .field-revert {
    font: inherit; font-size: 12px; background: none; border: 1px solid var(--divider);
    border-radius: 4px; padding: 2px 8px; cursor: pointer; color: var(--mid-gray);
    margin: 0 0 8px; display: inline-block;
  }
  .field-revert:hover { border-color: var(--primary-green); color: var(--primary-green); }

  /* Photo block */
  .photo-block { display: flex; gap: 14px; align-items: flex-start; margin-bottom: 14px; }
  .photo-actions { display: flex; flex-direction: column; gap: 6px; padding-top: 4px; }
  .photo-upload-btn {
    font-size: 13px; background: white; border: 1px solid var(--divider);
    border-radius: 4px; padding: 6px 12px; cursor: pointer; color: var(--dark);
    display: inline-block;
  }
  .photo-upload-btn:hover { border-color: var(--primary-green); color: var(--primary-green); }
  .photo-revert-btn {
    font: inherit; font-size: 12px; background: none; border: 1px solid var(--divider);
    border-radius: 4px; padding: 4px 10px; cursor: pointer; color: var(--mid-gray);
  }
  .photo-revert-btn:hover { border-color: var(--primary-green); color: var(--primary-green); }
  .photo-note { font-size: 12px; color: var(--mid-gray); font-style: italic; }

  /* Here with inline editor */
  .herewith-block { margin: 14px 0; padding: 14px; background: #fafafa; border-radius: 6px; border: 1px solid var(--divider); }
  .herewith-block h3 { margin: 0 0 10px; font-family: 'Bodoni Moda', serif; font-weight: 600; font-size: 14px; color: var(--primary-green); }
  .hw-row { display: grid; grid-template-columns: 1fr 140px 28px; gap: 8px; align-items: center; padding: 6px 0; border-bottom: 1px dashed var(--divider); }
  .hw-row:last-child { border-bottom: none; }
  .hw-row .hw-name { font-family: 'Bodoni Moda', serif; font-size: 14px; }
  .hw-row .hw-label { font-size: 12px; color: var(--mid-gray); }
  .hw-row .hw-remove { background: none; border: none; color: var(--danger); cursor: pointer; font-size: 14px; padding: 4px; border-radius: 3px; }
  .hw-row .hw-remove:hover { background: rgba(182,66,66,0.08); }
  .hw-empty { font-size: 13px; color: var(--mid-gray); font-style: italic; padding: 4px 0; }
  .hw-add { display: grid; grid-template-columns: 1fr 140px auto; gap: 8px; margin-top: 10px; }
  .hw-add input {
    font-family: 'Nunito', sans-serif; font-size: 13px;
    border: 1px solid var(--divider); border-radius: 4px; padding: 7px 9px; background: white;
  }
  .hw-add input:focus { outline: none; border-color: var(--primary-green); }
  .hw-add button {
    font: inherit; font-weight: 600; background: var(--primary-green); color: white;
    border: none; border-radius: 4px; padding: 0 14px; cursor: pointer;
  }
  .hw-add button:hover { background: var(--deep-green); }
  .memory-group textarea { background: white; }

  /* Relationships tab */
  .rel-form { background: white; border: 1px solid var(--divider); border-radius: 8px; padding: 18px; margin-bottom: 24px; display: grid; grid-template-columns: 1fr 1fr 1fr auto; gap: 10px; align-items: end; }
  .rel-form .field { margin: 0; }
  .rel-form button { font-family: 'Bodoni Moda', serif; font-size: 14px; padding: 8px 16px; background: var(--primary-green); color: white; border: none; border-radius: 4px; cursor: pointer; height: 36px; }
  .rel-form button:hover { background: var(--deep-green); }

  table.rels { width: 100%; border-collapse: collapse; background: white; border: 1px solid var(--divider); border-radius: 8px; overflow: hidden; }
  table.rels th, table.rels td { padding: 10px 14px; text-align: left; border-bottom: 1px solid var(--divider); font-family: 'Bodoni Moda', serif; font-size: 14px; }
  table.rels th { background: #fafafa; font-weight: 600; font-size: 12px; color: var(--mid-gray); text-transform: uppercase; letter-spacing: 0.06em; }
  table.rels td:last-child { width: 40px; text-align: right; }
  table.rels tr:last-child td { border-bottom: none; }
  .rel-delete { background: none; border: none; cursor: pointer; color: var(--danger); font-size: 15px; padding: 4px 8px; border-radius: 4px; }
  .rel-delete:hover { background: rgba(182,66,66,0.08); }
  .rels-empty { padding: 40px; text-align: center; color: var(--mid-gray); font-style: italic; }

  /* Guide tab — CMS for the San Miguel Guide section. Same look-and-feel
     as the People editor: white cards on cream, inline editable fields,
     unified Save button. */
  .guide-toolbar { display: flex; justify-content: flex-end; margin-bottom: 16px; }
  .guide-toolbar button,
  .guide-cat-add button {
    font-family: 'Bodoni Moda', serif; font-size: 14px;
    padding: 8px 16px; background: var(--primary-green); color: white;
    border: none; border-radius: 4px; cursor: pointer;
  }
  .guide-toolbar button:hover,
  .guide-cat-add button:hover { background: var(--deep-green); }
  .guide-cat-add { display: flex; justify-content: flex-end; margin: 8px 0 28px; }
  .guide-cat-add--all { margin-top: 24px; border-top: 1px dashed var(--divider); padding-top: 16px; }
  .guide-cat-add--all button { background: var(--mid-gray); }
  .guide-cat-add--all button:hover { background: var(--dark); }
  .guide-cat-header {
    font-family: 'Bodoni Moda', serif; font-size: 16px; font-weight: 600;
    color: var(--primary-green); margin: 24px 0 10px; padding-bottom: 6px;
    border-bottom: 1px solid var(--divider);
  }
  /* Toggle variant — the whole header row is a click target. The arrow
     rotates to indicate state. Sections collapse to header-only so long
     lists don't bury sections below them. */
  .guide-cat-header--toggle {
    display: flex; align-items: center; gap: 8px;
    width: 100%; background: transparent; border: none;
    border-bottom: 1px solid var(--divider);
    text-align: left; cursor: pointer; padding: 0 0 6px;
  }
  .guide-cat-header--toggle:hover { color: var(--deep-green); }
  .guide-cat-arrow {
    display: inline-block; transition: transform 0.15s ease;
    color: var(--mid-gray); font-size: 14px; line-height: 1;
  }
  .guide-cat-header--toggle.is-collapsed .guide-cat-arrow { transform: rotate(-90deg); }
  .guide-cat-title { flex: 1; }
  .guide-cat-count {
    font-weight: 400; color: var(--mid-gray); font-size: 12px;
  }
  .guide-cat-body.is-collapsed { display: none; }
  .guide-card {
    background: white; border: 1px solid var(--divider); border-radius: 8px;
    padding: 16px 18px; margin-bottom: 12px;
    display: grid; grid-template-columns: 1fr auto; gap: 10px 16px;
    align-items: start;
  }
  .guide-card .field { margin: 0; }
  .guide-card .field-row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  .guide-card .field-row .field { margin: 0; }
  .guide-card-grid { display: grid; gap: 10px; }
  .guide-delete {
    background: none; border: none; cursor: pointer;
    color: var(--danger); font-size: 15px; padding: 4px 8px; border-radius: 4px;
    align-self: start;
  }
  .guide-delete:hover { background: rgba(182,66,66,0.08); }
  .guide-empty { padding: 40px; text-align: center; color: var(--mid-gray); font-style: italic; }
  .guide-card.hidden-listing { opacity: 0.5; }
  .guide-card.hidden-listing input,
  .guide-card.hidden-listing textarea { background: #fafafa; }
  /* Shopping-only photo widget. Preview is a 3:2 thumb so the editor
     shows the same crop the live site will render. */
  .guide-photo-field { margin: 0; }
  .guide-photo-row { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
  .guide-photo-preview {
    width: 120px; aspect-ratio: 3 / 2; border-radius: 8px;
    border: 1px solid var(--divider); overflow: hidden; background: #fafafa;
    flex-shrink: 0;
  }
  .guide-photo-preview img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .guide-photo-preview--empty {
    display: flex; align-items: center; justify-content: center;
    color: var(--mid-gray); font-family: 'Nunito', sans-serif; font-size: 11px;
  }
  .guide-photo-actions { display: flex; gap: 8px; flex-wrap: wrap; }
  .guide-photo-pick {
    display: inline-flex; align-items: center; cursor: pointer;
    padding: 8px 12px; border-radius: 6px; background: var(--primary-green);
    color: white; font-family: 'Nunito', sans-serif; font-size: 13px; font-weight: 600;
    border: none;
  }
  .guide-photo-pick:hover { background: var(--deep-green); }
  .guide-photo-remove {
    padding: 8px 12px; border-radius: 6px; background: transparent;
    color: var(--accent-warm); font-family: 'Nunito', sans-serif; font-size: 13px; font-weight: 600;
    border: 1px solid var(--accent-warm); cursor: pointer;
  }
  .guide-photo-remove:hover { background: rgba(182,66,66,0.08); }
  .visibility-toggle {
    display: flex; align-items: center; gap: 6px;
    font-family: 'Nunito', sans-serif; font-size: 12px;
    color: var(--mid-gray); cursor: pointer; user-select: none;
    margin-bottom: 4px;
  }
  .visibility-toggle input[type="checkbox"] {
    margin: 0; cursor: pointer; accent-color: var(--primary-green);
  }
  .visibility-toggle.checked { color: var(--primary-green); }
</style>
</head>
<body>
<div id="app-shell">
  <div id="editor-column">
    <header>
      <h1>Los Invitados Editor</h1>
      <nav>
        <button id="tab-people" class="active" onclick="showTab('people')">People</button>
        <button id="tab-rels" onclick="showTab('rels')">Relationships</button>
        <button id="tab-guide" onclick="showTab('guide')">Guide</button>
        <button id="tab-coffee" onclick="showTab('coffee')">Coffee</button>
      </nav>
      <div class="signed-in">
        Signed in as
        <select id="signed-in-as" onchange="setSignedInAs(this.value)">
          <option value="elien">Elien</option>
          <option value="nima">Nima</option>
        </select>
        <button id="save-btn" onclick="save()" disabled>Save</button>
        <span id="status">loading…</span>
      </div>
    </header>
    <main>
      <section id="view-people" class="people-wrap"></section>
      <section id="view-rels" style="display:none;"></section>
      <section id="view-guide" style="display:none;"></section>
      <section id="view-coffee" style="display:none;"></section>
    </main>
  </div>
  <div id="preview-column">
    <div id="preview-bar">
      Preview
      <button onclick="reloadPreview()" title="Reload the preview now">Reload</button>
      <a href="/" target="_blank" rel="noopener">Open in new tab ↗</a>
    </div>
    <iframe id="preview-frame" src="/" title="Live preview of the wedding app"></iframe>
  </div>
</div>

<datalist id="guest-names"></datalist>
<datalist id="city-names"></datalist>
<datalist id="guide-categories"></datalist>
<datalist id="romantic-labels">
  <option value="spouses">
  <option value="wife">
  <option value="husband">
  <option value="partner">
  <option value="boyfriend">
  <option value="girlfriend">
  <option value="fiancé">
  <option value="fiancée">
  <option value="date">
</datalist>

<script>
  const ROMANTIC_LABELS = new Set([
    'spouse', 'spouses', 'wife', 'wives', 'husband', 'husbands',
    'partner', 'partners', 'boyfriend', 'girlfriend',
    'fiancé', 'fiancée', 'fiance', 'fiancee',
    'date', 'dates', 'plus one', 'plus-one', '+1',
    'lover', 'lovers', 'sweetheart', 'sweethearts', 'significant other',
  ]);

  const state = { guests: [], guestsByKey: {}, curation: null, selectedKey: null, signedInAs: 'elien', dirty: false, guide: [], knownCategories: [], cafes: [], collapsedGuideCats: new Set() };

  function setSignedInAs(who) {
    if (who !== 'elien' && who !== 'nima') return;
    state.signedInAs = who;
    localStorage.setItem('losInvitados.signedInAs', who);
    document.getElementById('signed-in-as').value = who;
  }

  function restoreSignedInAs() {
    const saved = localStorage.getItem('losInvitados.signedInAs');
    if (saved === 'elien' || saved === 'nima') {
      setSignedInAs(saved);
    }
  }

  async function unregisterWeddingAppSW() {
    // The wedding app (served at /) registers a service worker at sw.js
    // that does cache-first on every same-origin GET — including /editor
    // and /api/data. That means stale editor HTML + stale API responses
    // survive every refresh. Unregister it and purge caches so the
    // editor always hits the live server.
    if (!('serviceWorker' in navigator)) return;
    try {
      const regs = await navigator.serviceWorker.getRegistrations();
      await Promise.all(regs.map((r) => r.unregister()));
      if ('caches' in window) {
        const keys = await caches.keys();
        await Promise.all(keys.map((k) => caches.delete(k)));
      }
    } catch { /* best-effort */ }
  }

  async function loadData() {
    restoreSignedInAs();
    await unregisterWeddingAppSW();
    const res = await fetch('/api/data', { cache: 'no-store' });
    const data = await res.json();
    state.guests = data.guests;
    state.guestsByKey = Object.fromEntries(data.guests.map(g => [g.key, g]));
    state.curation = data.curation;
    state.knownCities = data.knownCities || [];
    state.guide = data.guide || [];
    state.knownCategories = data.knownCategories || [];
    state.cafes = data.cafes || [];
    populateDatalist();
    populateCityDatalist();
    populateCategoryDatalist();
    renderPeople();
    renderRels();
    renderGuide();
    renderCoffee();
    setStatus('loaded', 'saved');
  }

  function setStatus(msg, variant) {
    const el = document.getElementById('status');
    el.textContent = msg;
    el.className = variant || '';
  }

  function markDirty() {
    state.dirty = true;
    document.getElementById('save-btn').disabled = false;
    setStatus('unsaved changes', 'dirty');
  }

  function markClean() {
    state.dirty = false;
    document.getElementById('save-btn').disabled = true;
  }

  async function save() {
    if (!state.dirty) return;
    document.getElementById('save-btn').disabled = true;
    setStatus('saving…');
    try {
      const res = await fetch('/api/save', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...state.curation, guide: state.guide, cafes: state.cafes }),
      });
      const data = await res.json();
      if (data.ok) {
        markClean();
        setStatus('saved ✓', 'saved');
        reloadPreview();
      } else {
        setStatus('save failed: ' + data.error, 'dirty');
        document.getElementById('save-btn').disabled = false;
      }
    } catch (e) {
      setStatus('save failed: ' + e.message, 'dirty');
      document.getElementById('save-btn').disabled = false;
    }
  }

  function reloadPreview() {
    const frame = document.getElementById('preview-frame');
    if (!frame) return;
    // Re-set src with a cachebuster so service worker / HTTP cache can't
    // serve stale index.html after a fresh build.
    try {
      frame.contentWindow.location.reload();
    } catch {
      frame.src = '/?t=' + Date.now();
    }
  }

  // Warn before closing/refreshing with unsaved changes.
  window.addEventListener('beforeunload', (e) => {
    if (!state.dirty) return;
    e.preventDefault();
    e.returnValue = '';
  });

  function showTab(name) {
    document.getElementById('tab-people').classList.toggle('active', name === 'people');
    document.getElementById('tab-rels').classList.toggle('active', name === 'rels');
    document.getElementById('tab-guide').classList.toggle('active', name === 'guide');
    document.getElementById('tab-coffee').classList.toggle('active', name === 'coffee');
    document.getElementById('view-people').style.display = name === 'people' ? 'grid' : 'none';
    document.getElementById('view-rels').style.display = name === 'rels' ? 'block' : 'none';
    document.getElementById('view-guide').style.display = name === 'guide' ? 'block' : 'none';
    document.getElementById('view-coffee').style.display = name === 'coffee' ? 'block' : 'none';
  }

  function populateDatalist() {
    const dl = document.getElementById('guest-names');
    // Don't put hint text inside the option — Firefox's datalist matches
    // the typed input against the option's *label* (text content) when
    // present, not the value. A hint like "(no profile yet)" hid
    // form-less guests from substring searches like "ric" → Richard
    // Fisher, because "(no profile yet)" doesn't contain "ric".
    dl.innerHTML = state.guests.map(g =>
      `<option value="${escapeAttr(g.name)}"></option>`
    ).join('');
  }

  function populateCityDatalist() {
    const dl = document.getElementById('city-names');
    const cities = state.knownCities || [];
    dl.innerHTML = cities.map(c => `<option value="${escapeAttr(c)}">`).join('');
  }

  function populateCategoryDatalist() {
    const dl = document.getElementById('guide-categories');
    // Combine known DB categories with anything currently in unsaved state.
    const set = new Set(state.knownCategories || []);
    (state.guide || []).forEach(p => {
      const c = (p.category || '').trim();
      if (c) set.add(c);
    });
    const sorted = Array.from(set).sort((a, b) => a.localeCompare(b));
    dl.innerHTML = sorted.map(c => `<option value="${escapeAttr(c)}">`).join('');
  }

  function escapeHtml(s) {
    const div = document.createElement('div');
    div.textContent = s == null ? '' : String(s);
    return div.innerHTML;
  }
  function escapeAttr(s) { return escapeHtml(s).replace(/"/g, '&quot;'); }

  function nameToKey(nameInput) {
    const target = (nameInput || '').trim().toLowerCase();
    if (!target) return null;
    const exact = state.guests.find(g => g.name.toLowerCase() === target);
    if (exact) return exact.key;
    return null;
  }

  function avatarPhoto(g) {
    return g.photoUrl
      ? `<img src="/files/${encodeURIComponent(g.photoUrl)}" alt="" onerror="this.replaceWith(Object.assign(document.createElement('span'),{textContent:'${g.initials}'}))">`
      : escapeHtml(g.initials);
  }

  // ── People tab ───────────────────────────────────────────────────────────
  function renderPeople() {
    const wrap = document.getElementById('view-people');
    wrap.innerHTML = `
      <aside class="people-list">
        <div class="people-search"><input id="people-search" type="text" placeholder="Search guests…" oninput="renderPeopleList()"></div>
        <div class="people-items" id="people-items"></div>
      </aside>
      <div class="editor" id="people-editor"><div class="empty">Pick a guest on the left to edit.</div></div>
    `;
    renderPeopleList();
  }

  // Effective display name = unsaved display_name override (if any),
  // else the original CSV "First Last". Used by the sidebar + the H2
  // so the new name shows immediately as the admin types.
  function effectiveName(g) {
    const fo = state.curation.fieldOverrides?.[g.key] || {};
    const override = (fo.display_name?.value || '').trim();
    return override || g.csvName || g.name;
  }

  function renderPeopleList() {
    const q = (document.getElementById('people-search')?.value || '').trim().toLowerCase();
    const items = document.getElementById('people-items');
    const rows = state.guests.filter(g => !q || effectiveName(g).toLowerCase().includes(q));
    items.innerHTML = rows.map(g => {
      const loc = state.curation.locations[g.key] || {};
      const hasLoc = loc.currentCity || loc.hometown;
      const hasMem = state.curation.memories.some(m => m.guest === g.key && (m.text || '').trim());
      const hasRel = state.curation.relationships.some(r => r.a === g.key || r.b === g.key);
      const marks = [hasLoc && 'loc', hasMem && 'mem', hasRel && 'rel'].filter(Boolean).map(t => `<span>${t}</span>`).join('');
      const name = effectiveName(g);
      return `
        <div class="person-row ${g.key === state.selectedKey ? 'selected' : ''}" onclick="selectPerson('${g.key}')">
          <div class="person-thumb">${g.photoUrl ? `<img src="/${escapeAttr(g.photoUrl)}" alt="" onerror="this.replaceWith(Object.assign(document.createElement('span'),{textContent:'${g.initials}'}))">` : escapeHtml(g.initials)}</div>
          <div class="person-name">${escapeHtml(name)}</div>
          <div class="person-marks">${marks}</div>
        </div>`;
    }).join('');
  }

  function selectPerson(key) {
    state.selectedKey = key;
    renderPeopleList();
    renderEditor();
  }

  function renderEditor() {
    const editor = document.getElementById('people-editor');
    const g = state.guestsByKey[state.selectedKey];
    if (!g) { editor.innerHTML = '<div class="empty">Pick a guest on the left to edit.</div>'; return; }
    const loc = state.curation.locations[g.key] || {};
    const memsBySubject = Object.fromEntries(
      ['them', 'nima', 'elien', 'both'].map(s => [
        s, (state.curation.memories.find(m => m.guest === g.key && m.subject === s) || {}).text || ''
      ])
    );

    // Effective values for the three override-able form fields —
    // admin override if set, else whatever the guest wrote.
    const fo = state.curation.fieldOverrides?.[g.key] || {};
    const storyValue = (fo.how_we_know?.value ?? g.formStory ?? '');
    const leastValue = (fo.least_favorite?.value ?? g.formLeastFavorite ?? '');
    const photoOverridden = !!fo.photo_url;

    editor.innerHTML = `
      <div class="photo-block">
        <div class="photo" id="photo-preview-${g.key}">${photoMarkup(g)}</div>
        <div class="photo-actions">
          <label class="photo-upload-btn">
            Upload new photo
            <input type="file" accept="image/*" onchange="onPhotoPicked(event, '${g.key}')" style="display:none;">
          </label>
          ${photoOverridden
            ? `<button class="photo-revert-btn" onclick="revertPhoto('${g.key}')">Revert to guest-submitted photo</button>`
            : `<span class="photo-note">Using guest-submitted photo</span>`}
        </div>
      </div>
      <h2>${escapeHtml(effectiveName(g))}</h2>
      ${displayNameField(g)}
      <div class="field">
        <label>Current city</label>
        <input type="text" list="city-names" autocomplete="off" value="${escapeAttr(loc.currentCity || '')}" oninput="updateLocation('${g.key}', 'currentCity', this.value)">
      </div>
      <div class="field">
        <label>Grew up in</label>
        <input type="text" list="city-names" autocomplete="off" value="${escapeAttr(loc.hometown || '')}" oninput="updateLocation('${g.key}', 'hometown', this.value)">
      </div>
      ${formFieldGroup(g, 'how_we_know', 'How they know Elien and Nima', storyValue, !!fo.how_we_know, g.formStory)}
      ${formFieldGroup(g, 'least_favorite', "What they don't like about weddings", leastValue, !!fo.least_favorite, g.formLeastFavorite)}
      ${hereWithBlock(g)}
      ${memoryGroup(g, 'them', `A memory of ${g.name}`, memsBySubject.them)}
      ${memoryGroup(g, 'nima', 'A memory of Nima', memsBySubject.nima)}
      ${memoryGroup(g, 'elien', 'A memory of Elien', memsBySubject.elien)}
      ${memoryGroup(g, 'both', 'A memory of Elien and Nima', memsBySubject.both)}
      ${eventsBlock(g)}
      ${contactBlock(g)}
    `;
  }

  // ── Stay in Touch ────────────────────────────────────────────────────
  // One inline block per guest, rendered at the bottom of their editor
  // panel. Mirrors the public form's 7 fields (phone, email, IG, LI,
  // Twitter/X, BlueSky, Soundcloud). The "Source" line shows where the
  // current values came from — touching any input promotes the row to
  // source=signedInAs so the next merge_contacts run won't clobber it.
  function contactBlock(g) {
    const c = state.curation.contacts?.[g.key] || {};
    const src = c.source || '';
    const fields = [
      ['phone',      'Phone',         'e.g. (628) 213-9925',                'tel'],
      ['email',      'Email',         'name@example.com',                    'email'],
      ['instagram',  'Instagram',     'handle (no @)',                       'text'],
      ['linkedin',   'LinkedIn',      'https://www.linkedin.com/in/...',     'url'],
      ['twitter',    'Twitter/X',     'handle (no @)',                       'text'],
      ['bluesky',    'BlueSky',       'handle.bsky.social',                   'text'],
      ['soundcloud', 'Soundcloud',    'handle',                              'text'],
    ];
    const rows = fields.map(([key, label, placeholder, type]) => `
      <label style="display:block;font-size:12px;color:var(--mid-gray);margin-bottom:8px;">
        ${escapeHtml(label)}
        <input type="${type}"
               value="${escapeAttr(c[key] || '')}"
               placeholder="${escapeAttr(placeholder)}"
               oninput="updateContact('${g.key}', '${key}', this.value)"
               style="display:block;width:100%;margin-top:4px;padding:6px 8px;border:1px solid var(--divider);border-radius:4px;font:inherit;background:white;">
      </label>
    `).join('');
    const sourceNote = src === 'form'
      ? `<span class="field-note">Submitted by the guest via the Contact & Socials form. Editing any field below saves your changes; the form sync won't overwrite them.</span>`
      : (src
          ? `<span class="field-note">Hand-edited by <em>${escapeHtml(src)}</em>. Clear every field to delete and let the form sync take over again.</span>`
          : `<span class="field-note">No contact info yet. Add anything below — the guest's profile will show a "Stay in Touch" section as soon as one field is set.</span>`);
    return `
      <div class="memory-group">
        <h3>Stay in Touch</h3>
        ${sourceNote}
        <div style="margin-top:8px;">${rows}</div>
      </div>`;
  }

  function updateContact(key, field, value) {
    const v = (value || '').trim();
    const existing = state.curation.contacts[key] || {};
    // Touching any field promotes the row to signed-in user as the
    // source. Form-sourced data carried in via the merge is preserved
    // (we keep all the existing values) and any edits join it.
    const next = {...existing, [field]: v, source: state.signedInAs};
    // Don't store empty-everywhere rows in the in-memory state — keeps
    // the UI's source-of-truth tight, and the save logic will DELETE
    // any existing row at this key anyway.
    const allFields = ['phone','email','instagram','linkedin','twitter','bluesky','soundcloud'];
    if (allFields.every(f => !(next[f] || '').trim())) {
      delete state.curation.contacts[key];
    } else {
      state.curation.contacts[key] = next;
    }
    renderPeopleList();  // refresh the rose-bud badge on the sidebar tile if we expose it there
    markDirty();
  }

  // Display-name override field. The CSV import is the source of truth
  // for first_name / last_name (used for sorting + login fuzzy match);
  // this override only changes how the name renders on the guest's
  // profile + grid + relationship chips. Empty value clears the override
  // and the rendered name falls back to the CSV "First Last".
  function displayNameField(g) {
    const fo = state.curation.fieldOverrides?.[g.key] || {};
    const overrideVal = fo.display_name?.value ?? '';
    const isOverridden = !!fo.display_name;
    const csvName = g.csvName || '';
    return `
      <div class="field">
        <label>Display name</label>
        <input type="text" value="${escapeAttr(overrideVal)}"
               placeholder="${escapeAttr(csvName)}"
               oninput="updateDisplayName('${g.key}', this.value)">
        <span class="field-note">
          ${isOverridden
            ? `Custom name. Leave blank to use the CSV name (<em>${escapeHtml(csvName)}</em>).`
            : `Using the CSV-imported name. Type a new value to override (e.g. middle name, nickname).`}
        </span>
      </div>
    `;
  }

  function eventsBlock(g) {
    const fo = state.curation.fieldOverrides?.[g.key] || {};
    const thuEffective = (fo.rsvp_thursday?.value ?? g.formRsvpThursday ?? '').trim();
    const friEffective = (fo.rsvp_friday?.value ?? g.formRsvpFriday ?? '').trim();
    const thuOverridden = !!fo.rsvp_thursday;
    const friOverridden = !!fo.rsvp_friday;
    const opts = (selected) => ['', 'Attending', 'Declined', 'No Response']
      .map(v => `<option value="${escapeAttr(v)}"${v === selected ? ' selected' : ''}>${v || '— from RSVP sheet —'}</option>`)
      .join('');
    const preview = computeEventsText(thuEffective, friEffective) || 'Hidden — not attending either';
    return `
      <div class="memory-group">
        <h3>Events</h3>
        <span class="field-note">Drives the "Events" line at the bottom of the profile. Leave blank to use the guest's RSVP answer verbatim.</span>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:6px;">
          <label style="font-size:12px;color:var(--mid-gray);">
            Thursday Welcome Dinner
            <select style="display:block;margin-top:4px;width:100%;padding:6px;border:1px solid var(--divider);border-radius:4px;font:inherit;background:white;"
                    onchange="updateFieldOverride('${g.key}', 'rsvp_thursday', this.value)">
              ${opts(thuOverridden ? fo.rsvp_thursday.value : '')}
            </select>
            ${thuOverridden ? `<button class="field-revert" style="margin-top:4px;" onclick="revertFormField('${g.key}', 'rsvp_thursday')">Revert ↺ (RSVP says: ${escapeHtml(g.formRsvpThursday || 'No Response')})</button>` : ''}
          </label>
          <label style="font-size:12px;color:var(--mid-gray);">
            Friday Welcome Dinner
            <select style="display:block;margin-top:4px;width:100%;padding:6px;border:1px solid var(--divider);border-radius:4px;font:inherit;background:white;"
                    onchange="updateFieldOverride('${g.key}', 'rsvp_friday', this.value)">
              ${opts(friOverridden ? fo.rsvp_friday.value : '')}
            </select>
            ${friOverridden ? `<button class="field-revert" style="margin-top:4px;" onclick="revertFormField('${g.key}', 'rsvp_friday')">Revert ↺ (RSVP says: ${escapeHtml(g.formRsvpFriday || 'No Response')})</button>` : ''}
          </label>
        </div>
        <div class="field-note" style="margin-top:8px;">Profile will show: <em>${escapeHtml(preview)}</em></div>
      </div>`;
  }

  function computeEventsText(thu, fri) {
    const going_thu = (thu || '').toLowerCase() === 'attending';
    const going_fri = (fri || '').toLowerCase() === 'attending';
    if (going_thu && going_fri) return 'Thursday and Friday Welcome Dinners';
    if (going_thu) return 'Thursday Welcome Dinner';
    if (going_fri) return 'Friday Welcome Dinner';
    return '';
  }

  function photoMarkup(g) {
    return g.photoUrl
      ? `<img src="/${escapeAttr(g.photoUrl)}?t=${Date.now()}" alt="" onerror="this.parentElement.textContent='${g.initials}'">`
      : `<div style="display:flex;align-items:center;justify-content:center;width:100%;height:100%;color:white;font-size:22px;background:#8FB89E;">${escapeHtml(g.initials)}</div>`;
  }

  function formFieldGroup(g, field, title, value, isOverridden, formValue) {
    const note = isOverridden
      ? `<button class="field-revert" onclick="revertFormField('${g.key}', '${field}')" title="Revert to the guest's Google Form answer">Revert to guest's answer ↺</button>`
      : (formValue ? `<span class="field-note">From the guest's Google Form</span>`
                   : `<span class="field-note">Guest hasn't submitted the form yet</span>`);
    return `
      <div class="memory-group">
        <h3>${escapeHtml(title)}</h3>
        ${note}
        <textarea oninput="updateFieldOverride('${g.key}', '${field}', this.value)" placeholder="Leave blank to revert to the guest's answer">${escapeHtml(value)}</textarea>
      </div>`;
  }

  function partnersOf(key) {
    // Find every relationship where this guest is a or b AND the label is romantic.
    const out = [];
    state.curation.relationships.forEach((r, idx) => {
      const label = (r.label || '').toLowerCase();
      if (!ROMANTIC_LABELS.has(label)) return;
      const partnerKey = r.a === key ? r.b : (r.b === key ? r.a : null);
      if (!partnerKey) return;
      const partner = state.guestsByKey[partnerKey];
      out.push({
        idx,
        partnerKey,
        partnerName: partner?.name || partnerKey,
        label: r.label,
      });
    });
    return out;
  }

  function hereWithBlock(g) {
    const existing = partnersOf(g.key);
    const rows = existing.map(p => `
      <div class="hw-row">
        <div class="hw-name">${escapeHtml(p.partnerName)}</div>
        <div class="hw-label">${escapeHtml(p.label)}</div>
        <button class="hw-remove" onclick="removePartner(${p.idx})" title="Remove">✕</button>
      </div>`).join('');
    const empty = existing.length ? '' : `<div class="hw-empty">No romantic partners yet.</div>`;
    return `
      <div class="herewith-block">
        <h3>Here with</h3>
        ${rows}
        ${empty}
        <div class="hw-add">
          <input id="hw-add-name-${g.key}" list="guest-names" type="text" placeholder="Type a guest's name…" autocomplete="off">
          <input id="hw-add-label-${g.key}" list="romantic-labels" type="text" placeholder="optional (wife, girlfriend, …)" autocomplete="off">
          <button onclick="addPartner('${g.key}')">Add</button>
        </div>
      </div>`;
  }

  function addPartner(guestKey) {
    const nameInput = document.getElementById(`hw-add-name-${guestKey}`);
    const labelInput = document.getElementById(`hw-add-label-${guestKey}`);
    const partnerKey = nameToKey(nameInput.value);
    const typedLabel = (labelInput.value || '').trim();
    if (!partnerKey) { alert('Pick a guest from the suggestions.'); return; }
    if (partnerKey === guestKey) { alert('A guest cannot be their own partner.'); return; }
    // Label is optional — "Here with" already conveys romantic intent. If
    // Elien leaves it blank, we default to "partner" internally so the
    // render pipeline still surfaces this row under Here with. The chip
    // on the profile only shows the name, not the label, so "partner"
    // stays invisible unless Elien types something more specific here.
    if (typedLabel && !ROMANTIC_LABELS.has(typedLabel.toLowerCase())) {
      alert(`"${typedLabel}" isn't on the romantic-label list. Leave blank or pick from the dropdown (spouses, wife, partner, boyfriend, girlfriend, fiancé, date). For non-romantic connections, use the Relationships tab.`);
      return;
    }
    const label = typedLabel || 'partner';
    const dupe = state.curation.relationships.find(r =>
      (r.a === guestKey && r.b === partnerKey) || (r.a === partnerKey && r.b === guestKey));
    if (dupe) { alert('A relationship between these two already exists — edit it in the Relationships tab.'); return; }
    state.curation.relationships.push({ a: guestKey, b: partnerKey, label, source: state.signedInAs });
    nameInput.value = '';
    labelInput.value = '';
    renderEditor();
    renderPeopleList();
    renderRelTable();
    markDirty();
  }

  function removePartner(idx) {
    if (!confirm('Remove this partner?')) return;
    state.curation.relationships.splice(idx, 1);
    renderEditor();
    renderPeopleList();
    renderRelTable();
    markDirty();
  }

  function memoryGroup(g, subject, title, value) {
    return `
      <div class="memory-group">
        <h3>${escapeHtml(title)}</h3>
        <textarea oninput="updateMemory('${g.key}', '${subject}', this.value)" placeholder="Leave blank to hide this section">${escapeHtml(value)}</textarea>
      </div>`;
  }

  function updateLocation(key, field, value) {
    const v = value.trim();
    const loc = state.curation.locations[key] || {};
    if (v) loc[field] = v;
    else delete loc[field];
    if (Object.keys(loc).length) state.curation.locations[key] = loc;
    else delete state.curation.locations[key];
    // Keep the city autocomplete fresh as Elien types new places.
    if (v && !state.knownCities.some(c => c.toLowerCase() === v.toLowerCase())) {
      state.knownCities = [...state.knownCities, v].sort((a, b) => a.toLowerCase().localeCompare(b.toLowerCase()));
      populateCityDatalist();
    }
    renderPeopleList();
    markDirty();
  }

  function updateMemory(key, subject, value) {
    const v = value.trim();
    const idx = state.curation.memories.findIndex(m => m.guest === key && m.subject === subject);
    if (!v) {
      if (idx >= 0) state.curation.memories.splice(idx, 1);
    } else if (idx >= 0) {
      state.curation.memories[idx].text = v;
      state.curation.memories[idx].source = state.signedInAs;
    } else {
      state.curation.memories.push({ guest: key, subject, text: v, source: state.signedInAs });
    }
    renderPeopleList();
    markDirty();
  }

  function updateFieldOverride(key, field, value) {
    if (!state.curation.fieldOverrides) state.curation.fieldOverrides = {};
    const v = (value || '').trim();
    const g = state.guestsByKey[key];
    const formValue = (field === 'how_we_know' ? g.formStory
                    : field === 'least_favorite' ? g.formLeastFavorite
                    : field === 'photo_url' ? g.formPhotoUrl : '') || '';
    const bag = state.curation.fieldOverrides[key] || {};
    if (!v || v === (formValue || '').trim()) {
      // Empty or identical to form value = drop the override.
      delete bag[field];
    } else {
      bag[field] = { value: v, source: state.signedInAs };
    }
    if (Object.keys(bag).length) state.curation.fieldOverrides[key] = bag;
    else delete state.curation.fieldOverrides[key];
    markDirty();
  }

  function revertFormField(key, field) {
    updateFieldOverride(key, field, '');
    renderEditor();
    renderPeopleList();
  }

  // display_name lives in fieldOverrides like the others, but its baseline
  // is the CSV-imported "First Last" rather than a Google Form value, so
  // the dedupe logic uses csvName (not formValue).
  function updateDisplayName(key, value) {
    if (!state.curation.fieldOverrides) state.curation.fieldOverrides = {};
    const v = (value || '').trim();
    const g = state.guestsByKey[key];
    const csvName = (g && g.csvName) || '';
    const bag = state.curation.fieldOverrides[key] || {};
    if (!v || v === csvName) {
      delete bag['display_name'];
    } else {
      bag['display_name'] = { value: v, source: state.signedInAs };
    }
    if (Object.keys(bag).length) state.curation.fieldOverrides[key] = bag;
    else delete state.curation.fieldOverrides[key];
    markDirty();
    // Re-render the people list so the new name shows in the sidebar.
    renderPeopleList();
    // Update the H2 in the editor without losing focus on the input.
    const heading = document.querySelector('#editor h2');
    if (heading) heading.textContent = v || csvName;
  }

  function onPhotoPicked(event, key) {
    const file = event.target.files && event.target.files[0];
    if (!file) return;
    // Read file → base64 → POST to /api/photo → server saves to disk → we
    // get back the path and write it as a photo_url override.
    const reader = new FileReader();
    reader.onload = async () => {
      const dataUrl = reader.result;
      const base64 = dataUrl.split(',')[1] || '';
      setStatus('uploading photo…');
      try {
        const res = await fetch('/api/photo', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            guestKey: key,
            filename: file.name,
            mimetype: file.type || 'application/octet-stream',
            dataBase64: base64,
          }),
        });
        const data = await res.json();
        if (!data.ok) { setStatus('photo upload failed: ' + data.error, 'dirty'); return; }
        // Stash in the override bag — committed on next Save.
        if (!state.curation.fieldOverrides) state.curation.fieldOverrides = {};
        const bag = state.curation.fieldOverrides[key] || {};
        bag.photo_url = { value: data.path, source: state.signedInAs };
        state.curation.fieldOverrides[key] = bag;
        // Also update the effective photoUrl on the in-memory guest so the
        // preview renders immediately.
        state.guestsByKey[key].photoUrl = data.path;
        setStatus('photo uploaded — click Save to commit', 'dirty');
        markDirty();
        renderEditor();
        renderPeopleList();
      } catch (e) {
        setStatus('photo upload failed: ' + e.message, 'dirty');
      }
    };
    reader.readAsDataURL(file);
  }

  function revertPhoto(key) {
    updateFieldOverride(key, 'photo_url', '');
    // Restore the effective photoUrl in memory from the form value.
    const g = state.guestsByKey[key];
    g.photoUrl = g.formPhotoUrl || '';
    renderEditor();
    renderPeopleList();
  }

  // ── Relationships tab ────────────────────────────────────────────────────
  function renderRels() {
    const wrap = document.getElementById('view-rels');
    wrap.innerHTML = `
      <div class="rel-form">
        <div class="field">
          <label>Person A</label>
          <input list="guest-names" id="rel-a" type="text" placeholder="Start typing…">
        </div>
        <div class="field">
          <label>Person B</label>
          <input list="guest-names" id="rel-b" type="text" placeholder="Start typing…">
        </div>
        <div class="field">
          <label>Label (optional — blank = just name under "Here with")</label>
          <input id="rel-label" type="text" placeholder="e.g. sisters, dear friends, mom…">
        </div>
        <button onclick="addRelationship()">Add</button>
      </div>
      <div id="rel-table"></div>
    `;
    renderRelTable();
  }

  function renderRelTable() {
    const host = document.getElementById('rel-table');
    const rels = state.curation.relationships;
    if (!rels.length) {
      host.innerHTML = `<div class="rels-empty">No relationships yet. Add one above.</div>`;
      return;
    }
    const rows = rels.map((r, i) => {
      const a = state.guestsByKey[r.a]?.name || r.a;
      const b = state.guestsByKey[r.b]?.name || r.b;
      const source = r.source || '(none)';
      return `
        <tr>
          <td>${escapeHtml(a)}</td>
          <td>${escapeHtml(b)}</td>
          <td>${escapeHtml(r.label || '')}</td>
          <td>${escapeHtml(source)}</td>
          <td><button class="rel-delete" onclick="deleteRel(${i})" title="Delete">✕</button></td>
        </tr>`;
    }).join('');
    host.innerHTML = `
      <table class="rels">
        <thead><tr><th>Person A</th><th>Person B</th><th>Label</th><th>Source</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
  }

  function addRelationship() {
    const a = nameToKey(document.getElementById('rel-a').value);
    const b = nameToKey(document.getElementById('rel-b').value);
    const label = document.getElementById('rel-label').value.trim();
    if (!a) { alert('Person A: please pick a guest from the suggestions.'); return; }
    if (!b) { alert('Person B: please pick a guest from the suggestions.'); return; }
    if (a === b) { alert('Person A and Person B are the same guest.'); return; }
    const dupe = state.curation.relationships.find(r =>
      (r.a === a && r.b === b) || (r.a === b && r.b === a));
    if (dupe) { alert('A relationship between these two already exists.'); return; }
    state.curation.relationships.push({ a, b, label, source: state.signedInAs });
    document.getElementById('rel-a').value = '';
    document.getElementById('rel-b').value = '';
    document.getElementById('rel-label').value = '';
    renderRelTable();
    renderPeopleList();
    markDirty();
  }

  function deleteRel(i) {
    if (!confirm('Delete this relationship?')) return;
    state.curation.relationships.splice(i, 1);
    renderRelTable();
    renderPeopleList();
    markDirty();
  }

  // ── Guide tab — CMS for the San Miguel Guide section ─────────────────────
  // Data model: state.guide is an array of {category, name, note, address,
  // directionsUrl}. Rows with empty name are dropped server-side on save.
  function renderGuide() {
    const wrap = document.getElementById('view-guide');
    // Group places by category, preserving order within each group.
    const groups = new Map();
    state.guide.forEach((p, idx) => {
      const cat = (p.category || '').trim() || 'Uncategorized';
      if (!groups.has(cat)) groups.set(cat, []);
      groups.get(cat).push(idx);
    });
    // Always surface every known category from the DB so its "+ Add" button
    // is reachable even when the section is currently empty.
    (state.knownCategories || []).forEach(c => {
      if (c && !groups.has(c)) groups.set(c, []);
    });
    const sortedCats = Array.from(groups.keys()).sort((a, b) => a.localeCompare(b));

    let html = '';
    if (sortedCats.length === 0) {
      html += `<div class="guide-empty">No places yet. Click "+ Add place" below to start.</div>`;
    }
    for (const cat of sortedCats) {
      const indices = groups.get(cat);
      const collapsed = state.collapsedGuideCats.has(cat);
      const safeCat = escapeAttr(cat);
      // Header is the toggle. Click anywhere on the header row collapses
      // the section so long lists (Restaurants, Coffee) don't bury the
      // sections below them while you're editing one.
      html += `<button type="button" class="guide-cat-header guide-cat-header--toggle${collapsed ? ' is-collapsed' : ''}" onclick="toggleGuideCat('${safeCat}')" aria-expanded="${collapsed ? 'false' : 'true'}">
        <span class="guide-cat-arrow" aria-hidden="true">▾</span>
        <span class="guide-cat-title">${escapeHtml(cat)}</span>
        <span class="guide-cat-count">(${indices.length})</span>
      </button>`;
      html += `<div class="guide-cat-body${collapsed ? ' is-collapsed' : ''}">`;
      for (const idx of indices) {
        html += guidePlaceCard(idx);
      }
      // Per-section CTA — pre-fills the new card's category, focuses Name.
      html += `<div class="guide-cat-add">
        <button data-cat="${escapeAttr(cat)}" onclick="addPlaceToCategory(this.dataset.cat)">+ Add place to ${escapeHtml(cat)}</button>
      </div>`;
      html += `</div>`;
    }
    // Catch-all CTA — empty category, user picks via the dropdown on the new card.
    html += `<div class="guide-cat-add guide-cat-add--all">
      <button onclick="addPlaceToNewCategory()">+ Add place (new category)</button>
    </div>`;
    wrap.innerHTML = html;
  }

  // Toggle a Guide-CMS section's collapsed state. State lives on the
  // editor session — no server roundtrip; refresh resets to all-expanded.
  function toggleGuideCat(cat) {
    if (state.collapsedGuideCats.has(cat)) state.collapsedGuideCats.delete(cat);
    else state.collapsedGuideCats.add(cat);
    renderGuide();
  }

  function guidePlaceCard(idx) {
    const p = state.guide[idx];
    const visible = p.visible !== false; // default true
    const hasRooftop = p.hasRooftop === true;
    return `
      <div class="guide-card${visible ? '' : ' hidden-listing'}">
        <div class="guide-card-grid">
          <div style="display: flex; gap: 18px; align-items: center; flex-wrap: wrap;">
            <label class="visibility-toggle ${visible ? 'checked' : ''}">
              <input type="checkbox" ${visible ? 'checked' : ''}
                onchange="updatePlaceVisibility(${idx}, this.checked)">
              ${visible ? 'Visible on site' : 'Hidden from site'}
            </label>
            <label class="visibility-toggle ${hasRooftop ? 'checked' : ''}" title="Tag this place as having a rooftop — shows a small sun icon next to the name on the rendered site.">
              <input type="checkbox" ${hasRooftop ? 'checked' : ''}
                onchange="updatePlaceRooftop(${idx}, this.checked)">
              ${hasRooftop ? '☀ Rooftop' : 'Rooftop'}
            </label>
          </div>
          <div class="field-row">
            <div class="field">
              <label>Category</label>
              <select onchange="onCategorySelect(${idx}, this)">
                ${buildCategoryOptions(p.category)}
              </select>
            </div>
            <div class="field">
              <label>Name</label>
              <input type="text" value="${escapeAttr(p.name)}"
                oninput="updatePlace(${idx}, 'name', this.value)">
            </div>
          </div>
          <div class="field">
            <label>Note</label>
            <textarea oninput="updatePlace(${idx}, 'note', this.value)" rows="2">${escapeHtml(p.note)}</textarea>
          </div>
          <div class="field-row">
            <div class="field">
              <label>Address</label>
              <input type="text" value="${escapeAttr(p.address)}"
                oninput="updatePlace(${idx}, 'address', this.value)">
            </div>
            <div class="field">
              <label>Directions URL</label>
              <input type="url" value="${escapeAttr(p.directionsUrl)}"
                oninput="updatePlace(${idx}, 'directionsUrl', this.value)">
            </div>
          </div>
          ${(p.category || '').trim() === 'Shopping' ? guidePhotoField(idx) : ''}
        </div>
        <button class="guide-delete" onclick="deletePlace(${idx})" title="Delete this place">✕</button>
      </div>
    `;
  }

  // Photo upload row — Shopping cards only. Optional: a card with no
  // photo just renders normally on the live site. The photo only shows
  // in the open state of the place-card; collapsed view is unchanged.
  function guidePhotoField(idx) {
    const p = state.guide[idx];
    const path = (p.photoPath || '').trim();
    const previewSrc = path ? `/${path}.jpg?t=${Date.now()}` : '';
    return `
      <div class="field guide-photo-field">
        <label>Photo (optional, shown only when card is opened)</label>
        <div class="guide-photo-row">
          ${path
            ? `<div class="guide-photo-preview"><img src="${escapeAttr(previewSrc)}" alt="" onerror="this.style.display='none'"></div>`
            : `<div class="guide-photo-preview guide-photo-preview--empty">No photo</div>`}
          <div class="guide-photo-actions">
            <label class="guide-photo-pick">
              <input type="file" accept="image/*"
                onchange="onGuidePhotoPicked(event, ${idx})" style="display:none">
              <span>${path ? 'Replace photo' : 'Upload photo'}</span>
            </label>
            ${path
              ? `<button type="button" class="guide-photo-remove" onclick="removeGuidePhoto(${idx})">Remove</button>`
              : ''}
          </div>
        </div>
      </div>
    `;
  }

  function updatePlaceVisibility(idx, visible) {
    if (!state.guide[idx]) return;
    state.guide[idx].visible = visible;
    markDirty();
    renderGuide();
  }

  function updatePlaceRooftop(idx, hasRooftop) {
    if (!state.guide[idx]) return;
    state.guide[idx].hasRooftop = hasRooftop;
    markDirty();
    renderGuide();
  }

  function updatePlace(idx, field, value) {
    if (!state.guide[idx]) return;
    state.guide[idx][field] = value;
    markDirty();
  }

  // Shopping photo upload. Same pattern as the guest photo flow:
  // FileReader → base64 → /api/guide_photo → server crops + writes
  // .webp + .jpg → we stash the path on the row. Files only commit
  // to the DB on Save (the row's photoPath field), but the image
  // bytes hit disk immediately, which is fine: an orphan file from
  // an unsaved upload is harmless and easy to garbage-collect later.
  function onGuidePhotoPicked(event, idx) {
    const file = event.target.files && event.target.files[0];
    if (!file) return;
    const place = state.guide[idx];
    if (!place) return;
    const replacePath = (place.photoPath || '').trim();
    const reader = new FileReader();
    reader.onload = async () => {
      const dataUrl = reader.result;
      const base64 = (dataUrl || '').split(',')[1] || '';
      setStatus('uploading photo…');
      try {
        const res = await fetch('/api/guide_photo', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            filename: file.name,
            mimetype: file.type || 'application/octet-stream',
            dataBase64: base64,
            replacePath: replacePath,
          }),
        });
        const data = await res.json();
        if (!data.ok) { setStatus('photo upload failed: ' + data.error, 'dirty'); return; }
        state.guide[idx].photoPath = data.path;
        markDirty();
        setStatus('photo uploaded — click Save to commit', 'dirty');
        renderGuide();
      } catch (e) {
        setStatus('photo upload failed: ' + e.message, 'dirty');
      }
    };
    reader.readAsDataURL(file);
  }

  function removeGuidePhoto(idx) {
    if (!state.guide[idx]) return;
    state.guide[idx].photoPath = '';
    markDirty();
    renderGuide();
  }

  function addPlace() { addPlaceToCategory(''); }

  // Prompt for a brand-new category name, then drop a place into it.
  // Wired to the "+ Add place (new category)" CTA so a fresh section can
  // be spun up without first having to cheat by editing a row's <select>.
  function addPlaceToNewCategory() {
    const raw = prompt('New category name (e.g. "Bars", "Spas"):');
    if (raw == null) return;
    const cat = raw.trim();
    if (!cat) return;
    addPlaceToCategory(cat);
  }

  function addPlaceToCategory(cat) {
    state.guide.push({
      category: cat || '',
      name: '', note: '', address: '', directionsUrl: '',
      visible: true, hasRooftop: false, photoPath: '',
    });
    // Auto-expand the section we just added to so the user sees the
    // new card instead of a silent state mutation behind a collapsed header.
    if (cat) state.collapsedGuideCats.delete(cat);
    markDirty();
    renderGuide();
    // Focus the new row's Name field for instant entry. With Category now
    // a <select>, the first <input type="text"> in the last card is Name.
    requestAnimationFrame(() => {
      const cards = document.querySelectorAll('#view-guide .guide-card');
      const last = cards[cards.length - 1];
      if (!last) return;
      const name = last.querySelector('input[type="text"]');
      if (name) {
        name.focus();
        last.scrollIntoView({ behavior: 'smooth', block: 'center' });
      }
    });
  }

  // Build <option>s for the per-card Category <select>. Always includes
  // the row's current value (even if not in knownCategories) so a custom
  // category can never be silently dropped.
  function buildCategoryOptions(currentValue) {
    const cats = new Set(state.knownCategories || []);
    (state.guide || []).forEach(p => {
      const c = (p.category || '').trim();
      if (c) cats.add(c);
    });
    if (currentValue) cats.add(currentValue);
    const sorted = Array.from(cats).sort((a, b) => a.localeCompare(b));
    let opts = `<option value=""${!currentValue ? ' selected' : ''}>— pick a category —</option>`;
    for (const c of sorted) {
      const sel = c === currentValue ? ' selected' : '';
      opts += `<option value="${escapeAttr(c)}"${sel}>${escapeHtml(c)}</option>`;
    }
    opts += `<option value="__new__">+ New category…</option>`;
    return opts;
  }

  // Wired to the per-row Category <select>. The "+ New category…" option
  // prompts for a name and reassigns the row to it; everything else is a
  // straight value write.
  function onCategorySelect(idx, sel) {
    if (sel.value === '__new__') {
      const raw = prompt('New category name (e.g. "Bars", "Spas"):');
      const cat = (raw || '').trim();
      if (!cat) {
        // Snap the dropdown back to whatever the row was before.
        const current = (state.guide[idx] && state.guide[idx].category) || '';
        sel.value = current;
        return;
      }
      updatePlace(idx, 'category', cat);
    } else {
      updatePlace(idx, 'category', sel.value);
    }
    onPlaceCategoryChange();
  }

  function deletePlace(idx) {
    const p = state.guide[idx];
    const label = (p && p.name) ? `"${p.name}"` : 'this empty row';
    if (!confirm(`Are you sure you want to delete ${label}?\n\nThis can't be undone after you click Save.`)) return;
    state.guide.splice(idx, 1);
    markDirty();
    renderGuide();
  }

  // When the user blurs the category field, refresh the datalist (so a
  // newly-typed category becomes a suggestion) and re-render so the place
  // moves to its new group.
  function onPlaceCategoryChange() {
    populateCategoryDatalist();
    renderGuide();
  }

  // ── Coffee tab — CMS for the Coffee Map section ─────────────────────────
  // Same shape as Guide minus the category dimension. Cafes are a flat list,
  // ordered by id (first added shows first). Reuses the .guide-* CSS since
  // the layout is identical.
  function renderCoffee() {
    const wrap = document.getElementById('view-coffee');
    let html = '';
    if (state.cafes.length === 0) {
      html += `<div class="guide-empty">No cafes yet. Click "+ Add cafe" below to start.</div>`;
    } else {
      html += `<div class="guide-cat-header">Cafés <span style="font-weight:400; color: var(--mid-gray); font-size: 12px;">(${state.cafes.length})</span></div>`;
      state.cafes.forEach((_, idx) => {
        html += cafeCard(idx);
      });
    }
    html += `<div class="guide-cat-add">
      <button onclick="addCafe()">+ Add cafe</button>
    </div>`;
    wrap.innerHTML = html;
  }

  function cafeCard(idx) {
    const c = state.cafes[idx];
    const visible = c.visible !== false; // default true
    return `
      <div class="guide-card${visible ? '' : ' hidden-listing'}">
        <div class="guide-card-grid">
          <label class="visibility-toggle ${visible ? 'checked' : ''}">
            <input type="checkbox" ${visible ? 'checked' : ''}
              onchange="updateCafeVisibility(${idx}, this.checked)">
            ${visible ? 'Visible on site' : 'Hidden from site'}
          </label>
          <div class="field">
            <label>Name</label>
            <input type="text" value="${escapeAttr(c.name)}"
              oninput="updateCafe(${idx}, 'name', this.value)">
          </div>
          <div class="field">
            <label>Note</label>
            <textarea oninput="updateCafe(${idx}, 'note', this.value)" rows="2">${escapeHtml(c.note)}</textarea>
          </div>
          <div class="field-row">
            <div class="field">
              <label>Address</label>
              <input type="text" value="${escapeAttr(c.address)}"
                oninput="updateCafe(${idx}, 'address', this.value)">
            </div>
            <div class="field">
              <label>Directions URL</label>
              <input type="url" value="${escapeAttr(c.directionsUrl)}"
                oninput="updateCafe(${idx}, 'directionsUrl', this.value)">
            </div>
          </div>
        </div>
        <button class="guide-delete" onclick="deleteCafe(${idx})" title="Delete this cafe">✕</button>
      </div>
    `;
  }

  function updateCafeVisibility(idx, visible) {
    if (!state.cafes[idx]) return;
    state.cafes[idx].visible = visible;
    markDirty();
    renderCoffee();
  }

  function updateCafe(idx, field, value) {
    if (!state.cafes[idx]) return;
    state.cafes[idx][field] = value;
    markDirty();
  }

  function addCafe() {
    state.cafes.push({ name: '', note: '', address: '', directionsUrl: '', visible: true });
    markDirty();
    renderCoffee();
    requestAnimationFrame(() => {
      const cards = document.querySelectorAll('#view-coffee .guide-card');
      const last = cards[cards.length - 1];
      if (last) {
        const name = last.querySelector('.field input');
        if (name) name.focus();
      }
    });
  }

  function deleteCafe(idx) {
    const c = state.cafes[idx];
    const label = (c && c.name) ? `"${c.name}"` : 'this empty row';
    if (!confirm(`Are you sure you want to delete ${label}?\n\nThis can't be undone after you click Save.`)) return;
    state.cafes.splice(idx, 1);
    markDirty();
    renderCoffee();
  }

  loadData();
</script>
</body>
</html>
"""


# ── Entry ──────────────────────────────────────────────────────────────────

def main():
    if not os.path.exists(DB_PATH):
        raise SystemExit(f"wedding.db not found at {DB_PATH}")

    ensure_schema()

    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}/editor"
    print(f"Los Invitados editor running at {url}")
    print("Writes the curation tables in wedding.db. Ctrl+C to stop.")

    # Open the browser in a background thread so the server is ready first.
    threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
