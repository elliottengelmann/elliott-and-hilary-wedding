#!/usr/bin/env python3
"""
Sync guest data from the Google Form into wedding.db.

This wedding has no RSVP tracking and no seating chart — the guest
directory is driven purely by the Google Form: **anyone who fills it
out gets a profile.** There is no separate guest-list export to
cross-reference against.

1. form_responses.csv          — the form's answers: name, pronouns,
                                  "how you know us", karaoke song, photo
2. contact_form_responses.csv  — optional; post-wedding contact info
                                  (phone, email, socials) from a
                                  separate "Get to Know You" form

Reads from local CSVs. To refresh: pull the latest rows from the
linked Google Sheet (via the Google Drive connector — never
curl/wget/gdown, the sheet is private) and write them out as
form_responses.csv, then run this script.

IMPORTANT — DO NOT DROP THE CURATION TABLES.

wedding.db also contains four tables managed by `scripts/edit_guests.py`:
    guest_locations, guest_memories, relationships, guest_contacts

These hold Hilary/Elliott's hand-authored curation (current city,
hometown, memories, "Here with" pairings) plus form-sourced contact
info that survives every merge. They're keyed by normalized
first_last names. This script only DROPs `guests` — the curation
tables stay intact on every run. Don't change that.

The `guest_contacts` table is populated from contact_form_responses.csv
(source='form') and from manual edits in edit_guests.py (source='elien'
or 'nima'). On each merge run, form-sourced rows are refreshed from the
CSV; rows tagged 'elien' or 'nima' are NEVER overwritten by the form sync.

See CLAUDE.md → "The guest form and how fields map" for the full picture.
"""

import csv
import datetime
import os
import re
import sqlite3

DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(DIR, "wedding.db")
FORM_CSV = os.path.join(DIR, "form_responses.csv")
CONTACT_FORM_CSV = os.path.join(DIR, "contact_form_responses.csv")


def normalize(name):
    """Normalize a name for fuzzy matching: lowercase, strip whitespace."""
    return " ".join(name.strip().lower().split())


# ── Manual name mappings ────────────────────────────────────────────────
# Keyed by normalized (first, last) as submitted on the form. Use this
# only to unify two form submissions from the same person (e.g. a typo
# fix re-submission) onto one canonical (first, last) — empty until
# Hilary/Elliott need one. Do NOT auto-add by inference.
FORM_NAME_MAP = {}


def clean_guest_row(first, last):
    """Clean and validate a guest row. Returns (first, last) or None to skip."""
    if not first and not last:
        return None
    # Fix names where full name ended up in first_name with empty last_name
    if " " in first and not last:
        parts = first.rsplit(" ", 1)
        first, last = parts[0], parts[1]
    return first, last


GUEST_IMAGES_DIR = os.path.join(DIR, "images", "guests")


def _build_local_photo_index():
    """Scan images/guests/ and return {name_stem: relative_path}."""
    index = {}
    if not os.path.isdir(GUEST_IMAGES_DIR):
        return index
    for fname in os.listdir(GUEST_IMAGES_DIR):
        stem, _ = os.path.splitext(fname)
        index[stem] = f"images/guests/{fname}"
    return index


def resolve_photo(first, last, raw_url, local_index):
    """Return a local image path if available, otherwise the raw URL."""
    stem = f"{first}_{last}".lower().replace(" ", "_")
    if stem in local_index:
        return local_index[stem]
    return raw_url


def _first_matching_column(row, *prefixes):
    """Return the first cell whose column header starts with one of the
    given prefixes (case-insensitive, trims each header). Used because
    the Google Form question text drifts over time and we want to match
    on the stable lead of each question rather than exact full text."""
    for header, value in row.items():
        if header is None:
            continue
        h = header.strip().lower()
        for p in prefixes:
            if h.startswith(p.lower()):
                return (value or "").strip()
    return ""


def read_form_responses():
    """Read form_responses.csv — the single source of truth for the
    guest list. Returns {(first_norm, last_norm): {...}}, keyed so a
    later duplicate submission (e.g. a typo re-submit) overwrites an
    earlier one, since Google Sheets appends new rows at the bottom."""
    responses = {}
    with open(FORM_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            first = row.get("First Name", "").strip()
            last = row.get("Last Name", "").strip()
            cleaned = clean_guest_row(first, last)
            if not cleaned:
                continue
            first, last = cleaned
            key = (normalize(first), normalize(last))
            key = FORM_NAME_MAP.get(key, key)
            responses[key] = {
                "first_raw":      first,
                "last_raw":       last,
                "email":          _first_matching_column(row, "Email Address", "Email"),
                "how_we_know":    _first_matching_column(row, "How do you know"),
                "photo_url":      _first_matching_column(row, "Share a photo", "Please Upload a Photo", "Photo URL"),
                # Hilary & Elliott's form asks for a go-to karaoke song instead of a
                # "least favorite thing about weddings". We carry it in the existing
                # least_favorite column; the profile label reads as karaoke (build.py).
                "least_favorite": _first_matching_column(row, "What is one of your go-to karaoke", "Least Favorite"),
                "pronouns":       _first_matching_column(row, "Pronoun"),
                "form_current_city": _first_matching_column(row, "What city or town do you live in now"),
                "form_hometown":     _first_matching_column(row, "Where did you grow up"),
                "form_memory":       _first_matching_column(row, "If you'd like share a memory", "If you'd like to share a memory"),
            }
    return responses


def merge_and_write(form_responses):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    local_photos = _build_local_photo_index()

    c.execute("DROP TABLE IF EXISTS guests")
    c.execute("""
        CREATE TABLE guests (
            id              INTEGER PRIMARY KEY,
            first_name      TEXT,
            last_name       TEXT,
            full_name       TEXT,
            title           TEXT,
            suffix          TEXT,
            email           TEXT,
            group_house     TEXT,
            guest_name      TEXT,
            address         TEXT,
            phone           TEXT,
            person          TEXT,
            food_choice     TEXT,
            food_note       TEXT,
            rsvp_status     TEXT,
            rsvp_thursday   TEXT,
            rsvp_friday     TEXT,
            rsvp_wedding    TEXT,
            rsvp_sunday     TEXT,
            is_plus_one     INTEGER DEFAULT 0,
            how_we_know     TEXT,
            photo_url       TEXT,
            least_favorite  TEXT,
            form_current_city TEXT,
            form_hometown     TEXT,
            form_memory       TEXT,
            pronouns          TEXT,
            initials        TEXT
        )
    """)

    with_photo = 0
    with_story = 0

    for key in sorted(form_responses):
        form = form_responses[key]
        first, last = form["first_raw"], form["last_raw"]

        initials = ""
        if last and first:
            initials = last[0].upper() + first[0].upper()

        photo = resolve_photo(first, last, form.get("photo_url", ""), local_photos)
        if photo:
            with_photo += 1
        if form.get("how_we_know", ""):
            with_story += 1

        c.execute("""
            INSERT INTO guests (
                first_name, last_name, full_name, title, suffix,
                email, group_house, guest_name, address, phone,
                person, food_choice, food_note,
                rsvp_status, rsvp_thursday, rsvp_friday, rsvp_wedding, rsvp_sunday,
                is_plus_one,
                how_we_know, photo_url, least_favorite,
                form_current_city, form_hometown, form_memory,
                pronouns,
                initials
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            first, last, f"{first} {last}".strip(), "", "",
            form.get("email", ""), "", "", "", "",
            "", "", "",
            # No RSVP concept for this wedding — filling out the form
            # means you're coming. rsvp_thursday/friday/wedding/sunday
            # stay blank on purpose: build.py's per-event RSVP chips
            # (a leftover from the template) only render if these are
            # set, and this wedding shows every guest the full schedule
            # instead.
            "Attending", "", "", "", "",
            0,
            form.get("how_we_know", ""),
            photo,
            form.get("least_favorite", ""),
            form.get("form_current_city", ""),
            form.get("form_hometown", ""),
            form.get("form_memory", ""),
            form.get("pronouns", ""),
            initials,
        ))

    conn.commit()

    c.execute("SELECT COUNT(*) FROM guests")
    total = c.fetchone()[0]
    conn.close()

    print(f"Synced {total} guest(s) from the form into wedding.db")
    print(f"  {with_story} have a \"how we know each other\" story")
    print(f"  {with_photo} have a photo")


# ── Contact-form ingestion ──────────────────────────────────────────────
# Reads contact_form_responses.csv (post-wedding contact info: phone,
# email, Instagram, LinkedIn, Twitter, BlueSky, Soundcloud) and writes
# matched rows into wedding.db's `guest_contacts` table. This is a
# separate, optional form from the main guest form above (see CLAUDE.md
# — the "Get to Know You" form for the guest Facebook feature).
#
# Free-text form fields are messy in the wild — handles vs. @handles
# vs. full URLs with tracking params, phone formatting variants, "N/A"
# entries. The normalize_* helpers below canonicalize on ingest so the
# rendering layer can assume clean values.

# Maps a normalized full name (lowercased, single-spaced) to a known
# (first_norm, last_norm) guest key. Used when fuzzy splitting the
# contact form's single "Full Name" field can't reach an existing guest
# row on its own — e.g. married-name drift, typos, first-name-only
# submissions. Keep entries here narrow and verified by Hilary/Elliott;
# do NOT auto-add by inference. Empty until they need one.
CONTACT_FORM_NAME_MAP = {}


def to_guest_key(first, last):
    """Build the canonical guest_key used across curation tables —
    `first_last` lowercased, spaces collapsed to underscores. Matches
    build.guest_key() and scripts/edit_guests.py exactly."""
    return f"{(first or '').strip().lower()}_{(last or '').strip().lower()}".replace(" ", "_")


def _strip_url_query(url):
    """Drop ?... query string and trailing slash. UTM and igsh tracking
    params get carried into LinkedIn/Instagram URLs when people share
    from their phone; render cleanly without them."""
    if not url:
        return ""
    base = url.split("?", 1)[0].split("#", 1)[0]
    return base.rstrip("/")


def normalize_phone(raw):
    """Keep only digits and a single leading '+'. Display formatting
    happens in build.py; storage is canonical so tel: links resolve
    consistently across formats like '(843) 757-3809', '917-481-...',
    '+14168784164', '0046760448922'."""
    s = (raw or "").strip()
    if not s or s.lower() in ("n/a", "na", "none"):
        return ""
    plus = "+" if s.lstrip().startswith("+") else ""
    digits = re.sub(r"\D", "", s)
    return f"{plus}{digits}" if digits else ""


def normalize_email(raw):
    s = (raw or "").strip().lower()
    if not s or s in ("n/a", "na", "none"):
        return ""
    return s


def normalize_instagram(raw):
    """Return a bare handle (no @, no URL). Builds links from this at
    render time. Handles all observed input shapes:
        - bare handle: 'viksit', 'theloristori'
        - @handle:     '@cndeboo'
        - full URL:    'https://www.instagram.com/elinor...?igsh=...'
        - domain-prefixed without scheme: 'Instagram.com/sepandsep_'
        - 'N/A' or empty → ''"""
    s = (raw or "").strip()
    if not s or s.lower() in ("n/a", "na", "none"):
        return ""
    m = re.match(r"(?:https?://)?(?:www\.)?instagram\.com/([^/?#\s]+)", s, re.IGNORECASE)
    if m:
        return m.group(1).lstrip("@").strip()
    return s.lstrip("@").strip()


def normalize_linkedin(raw):
    """Return a canonical https://www.linkedin.com/... URL with
    tracking params stripped. Handles:
        - full https URL (preferred form, just strips ?utm_...)
        - 'www.linkedin.com/in/foo' (no scheme)
        - 'LinkedIn.com/u/Foo' (alt path)
        - '/in/foo' (path-only, no host)
        - bare slug 'viksit' → assume /in/<slug>"""
    s = (raw or "").strip()
    if not s or s.lower() in ("n/a", "na", "none"):
        return ""
    if re.match(r"^https?://", s, re.IGNORECASE):
        return _strip_url_query(s)
    if re.match(r"^(?:www\.)?linkedin\.com/", s, re.IGNORECASE):
        return _strip_url_query("https://" + s.lstrip("/"))
    if s.startswith("/"):
        # Path-only form: '/in/annaspisak' → 'https://www.linkedin.com/in/annaspisak'
        return _strip_url_query("https://www.linkedin.com" + s)
    if re.match(r"^[A-Za-z0-9_-]+$", s):
        return f"https://www.linkedin.com/in/{s}"
    return s


def normalize_twitter(raw):
    """Bare handle, no @. Handles twitter.com/... and x.com/..."""
    s = (raw or "").strip()
    if not s or s.lower() in ("n/a", "na", "none"):
        return ""
    m = re.match(r"(?:https?://)?(?:www\.)?(?:twitter|x)\.com/([^/?#\s]+)", s, re.IGNORECASE)
    if m:
        return m.group(1).lstrip("@").strip()
    return s.lstrip("@").strip()


def normalize_bluesky(raw):
    """Bare handle (often with .bsky.social). Strips bsky.app/profile/ URLs."""
    s = (raw or "").strip()
    if not s or s.lower() in ("n/a", "na", "none"):
        return ""
    m = re.match(r"(?:https?://)?(?:www\.)?bsky\.app/profile/([^/?#\s]+)", s, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return s.lstrip("@").strip()


def normalize_soundcloud(raw):
    """Bare handle, no URL prefix."""
    s = (raw or "").strip()
    if not s or s.lower() in ("n/a", "na", "none"):
        return ""
    m = re.match(r"(?:https?://)?(?:www\.)?soundcloud\.com/([^/?#\s]+)", s, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return s.lstrip("@").strip()


def parse_form_timestamp(raw):
    """Convert Google Forms' '5/11/2026 10:06:44' to ISO
    '2026-05-11 10:06:44' so timestamps sort lexicographically and the
    'newest submission wins' rule on re-submits works without parsing
    at compare time."""
    s = (raw or "").strip()
    if not s:
        return ""
    try:
        dt = datetime.datetime.strptime(s, "%m/%d/%Y %H:%M:%S")
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return s


def read_contact_form_responses():
    """Read contact_form_responses.csv, normalize every field, return
    a list of dicts in submission order. Skips rows with an empty name."""
    if not os.path.exists(CONTACT_FORM_CSV):
        return []
    rows = []
    with open(CONTACT_FORM_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            full_name = _first_matching_column(row, "Your Full Name", "Full Name", "Name").strip()
            if not full_name:
                continue
            rows.append({
                "full_name":    full_name,
                "submitted_at": parse_form_timestamp(_first_matching_column(row, "Timestamp")),
                "phone":        normalize_phone(_first_matching_column(row, "Phone")),
                "email":        normalize_email(_first_matching_column(row, "email", "Email")),
                "instagram":    normalize_instagram(_first_matching_column(row, "Instagram")),
                "linkedin":     normalize_linkedin(_first_matching_column(row, "LinkedIn")),
                "twitter":      normalize_twitter(_first_matching_column(row, "Twitter")),
                "bluesky":      normalize_bluesky(_first_matching_column(row, "BlueSky", "Bluesky")),
                "soundcloud":   normalize_soundcloud(_first_matching_column(row, "Soundcloud", "SoundCloud")),
            })
    return rows


def _candidate_splits(full_norm):
    """Yield (first_norm, last_norm) candidates from a normalized full
    name. Tries every plausible split so a multi-word last name can
    reach whichever split the guest table actually has."""
    tokens = full_norm.split()
    if not tokens:
        return
    if len(tokens) == 1:
        yield (tokens[0], "")
        return
    # Try every split point: first N tokens as first name, rest as last.
    for i in range(1, len(tokens)):
        yield (" ".join(tokens[:i]), " ".join(tokens[i:]))
    # Also yield the lone-first-name shape, for "sep" → ("sep", "")
    yield (tokens[0], "")


def match_contact_name(full_name, known_keys):
    """Resolve a contact-form 'Full Name' to a guest_key (the FK to
    wedding.db's curation tables). Returns (guest_key, reason) on hit
    or (None, reason) on miss. `reason` is a short string useful for
    logging — 'map', 'split', 'form_map', 'unmatched'."""
    full_norm = normalize(full_name)
    if not full_norm:
        return (None, "empty")

    # 1) Explicit override map — for cases that fuzzy splitting can't reach.
    if full_norm in CONTACT_FORM_NAME_MAP:
        first, last = CONTACT_FORM_NAME_MAP[full_norm]
        key = to_guest_key(first, last)
        if key in known_keys:
            return (key, "map")

    # 2) Try every plausible (first, last) split against known keys
    #    directly, then against FORM_NAME_MAP for the existing form-side
    #    aliases (typo re-submits etc.).
    for first, last in _candidate_splits(full_norm):
        key = to_guest_key(first, last)
        if key in known_keys:
            return (key, "split")
        aliased = FORM_NAME_MAP.get((first, last))
        if aliased:
            akey = to_guest_key(*aliased)
            if akey in known_keys:
                return (akey, "form_map")
    return (None, "unmatched")


def merge_contacts(contact_rows):
    """Write contact-form responses to wedding.db's `guest_contacts`
    table. Idempotent: creates the table if missing, then upserts each
    row keyed by guest_key. Form-sourced rows are refreshed on every
    run; rows hand-edited via edit_guests.py (source='elien'/'nima')
    are preserved untouched."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Schema lives here (not in edit_guests.py) so merge_guests.py is
    # self-sufficient on a fresh checkout — but edit_guests.py also
    # ensures it exists with CREATE TABLE IF NOT EXISTS, so either
    # script can be the "first to run."
    c.execute("""
        CREATE TABLE IF NOT EXISTS guest_contacts (
            guest_key    TEXT PRIMARY KEY,
            phone        TEXT DEFAULT '',
            email        TEXT DEFAULT '',
            instagram    TEXT DEFAULT '',
            linkedin     TEXT DEFAULT '',
            twitter      TEXT DEFAULT '',
            bluesky      TEXT DEFAULT '',
            soundcloud   TEXT DEFAULT '',
            source       TEXT NOT NULL CHECK(source IN ('form', 'elien', 'nima')),
            submitted_at TEXT DEFAULT '',
            updated_at   TEXT NOT NULL
        )
    """)

    # Build set of valid keys from the freshly-rebuilt guests table.
    known_keys = set()
    for first, last in c.execute("SELECT first_name, last_name FROM guests"):
        known_keys.add(to_guest_key(first, last))

    inserted = 0
    updated = 0
    skipped_manual = 0
    unmatched = []
    now_iso = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    for row in contact_rows:
        key, _reason = match_contact_name(row["full_name"], known_keys)
        if not key:
            unmatched.append(row["full_name"])
            continue

        existing = c.execute(
            "SELECT source FROM guest_contacts WHERE guest_key = ?", (key,)
        ).fetchone()
        if existing and existing[0] in ("elien", "nima"):
            skipped_manual += 1
            continue

        # ON CONFLICT...DO UPDATE only writes when source='form' AND the
        # new submission is at least as recent as the stored one. The
        # second clause makes re-imports of an out-of-order CSV safe:
        # if the CSV is ever sorted descending or shuffled, the newest
        # submission still wins.
        c.execute("""
            INSERT INTO guest_contacts (
                guest_key, phone, email, instagram, linkedin,
                twitter, bluesky, soundcloud,
                source, submitted_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'form', ?, ?)
            ON CONFLICT(guest_key) DO UPDATE SET
                phone        = excluded.phone,
                email        = excluded.email,
                instagram    = excluded.instagram,
                linkedin     = excluded.linkedin,
                twitter      = excluded.twitter,
                bluesky      = excluded.bluesky,
                soundcloud   = excluded.soundcloud,
                source       = 'form',
                submitted_at = excluded.submitted_at,
                updated_at   = excluded.updated_at
            WHERE guest_contacts.source = 'form'
              AND COALESCE(excluded.submitted_at, '')
                  >= COALESCE(guest_contacts.submitted_at, '')
        """, (
            key, row["phone"], row["email"], row["instagram"], row["linkedin"],
            row["twitter"], row["bluesky"], row["soundcloud"],
            row["submitted_at"], now_iso,
        ))
        if existing is None:
            inserted += 1
        else:
            updated += 1

    conn.commit()

    c.execute("SELECT COUNT(*) FROM guest_contacts")
    total = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM guest_contacts WHERE source = 'form'")
    form_count = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM guest_contacts WHERE source IN ('elien', 'nima')")
    manual_count = c.fetchone()[0]
    conn.close()

    print(f"Contacts: {total} total ({form_count} form, {manual_count} hand-edited)")
    print(f"  {inserted} new from form, {updated} updated from form, {skipped_manual} skipped (hand-edited)")
    if unmatched:
        print(f"  {len(unmatched)} contact submission(s) with no guest match:")
        for name in unmatched:
            print(f"    - {name!r}")


def main():
    if not os.path.exists(FORM_CSV):
        print("Missing form_responses.csv")
        return

    form_responses = read_form_responses()
    merge_and_write(form_responses)

    # Contact form is optional — skip silently if the CSV isn't there.
    if os.path.exists(CONTACT_FORM_CSV):
        contact_rows = read_contact_form_responses()
        merge_contacts(contact_rows)
    else:
        print("Contacts: contact_form_responses.csv not present, skipping")


if __name__ == "__main__":
    main()
