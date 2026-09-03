#!/usr/bin/env python3
"""
Merge guest data from multiple sources into the wedding.db SQLite database.

1. export.csv                  — Most up-to-date guest list & per-event RSVPs
2. full_list.csv               — Supplementary RSVP sheet
3. master_list.csv             — Contact details: email, food choice, address
4. form_responses.csv          — "How do you know us", photo URL, fun answers
5. contact_form_responses.csv  — Post-wedding contact info (phone, email, socials)

Reads from local CSVs. To refresh, re-export from Google Sheets / the app.

IMPORTANT — DO NOT DROP THE CURATION TABLES.

wedding.db also contains four tables managed by `scripts/edit_guests.py`:
    guest_locations, guest_memories, relationships, guest_contacts

These hold Elien's / Nima's hand-authored curation (current city,
hometown, memories, Here-with pairings) plus form-sourced contact
info that survives every merge. They're keyed by normalized
first_last names. This script only DROPs `guests` — the curation
tables stay intact on every run. Don't change that.

The `guest_contacts` table is populated from contact_form_responses.csv
(source='form') and from manual edits in edit_guests.py (source='elien'
or 'nima'). On each merge run, form-sourced rows are refreshed from the
CSV; rows tagged 'elien' or 'nima' are NEVER overwritten by the form sync.

See CLAUDE.md → "Curation lives in wedding.db" for the full picture.
"""

import csv
import datetime
import os
import re
import sqlite3

DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(DIR, "wedding.db")
EXPORT_CSV = os.path.join(DIR, "export.csv")
FULL_LIST_CSV = os.path.join(DIR, "full_list.csv")
MASTER_CSV = os.path.join(DIR, "master_list.csv")
FORM_CSV = os.path.join(DIR, "form_responses.csv")
CONTACT_FORM_CSV = os.path.join(DIR, "contact_form_responses.csv")


def normalize(name):
    """Normalize a name for fuzzy matching: lowercase, strip whitespace."""
    return " ".join(name.strip().lower().split())


# ── Manual name mappings ────────────────────────────────────────────────
# Form response name -> export/full list name (for mismatched names)
FORM_NAME_MAP = {
    ("alexa", "meyer"):                   ("alexandra", "meyer"),
    ("alice", "ollier"):                   ("alice", "george"),
    ("ashutosh", "desai"):                 ("ashu", "desai"),
    ("asma", "amani"):                     ("asma", "ahmed"),
    ("lisa", "galano friedman"):           ("lisa galano", "friedman"),
    ("barbara", "soalheiro"):              ("barbara", "gwercman"),
    ("joshua", "katz"):                    ("josh", "katz"),
    ("jessica", "yu"):                     ("jess", "yu"),
    ("lena", "elkousy"):                   ("lena", "elsouky"),
    ("liz", "sia"):                        ("elizabeth", "sia"),
    ("mariam", "aghdaee"):                 ("maryam", "aghdaee"),
    ("nina", "remiker-scheinman"):         ("nina", "scheinman"),
    ("olivia", "menezes"):                 ("olivia", "benjamin"),
    ("omat", "elsayed"):                   ("omar", "elsayed"),
    ("sani", "hussain"):                   ("sanaria", "hussain"),
    ("sepand", "norouzi"):                 ("sep", "norouzi"),
    ("seth", "bannon \u2728"):             ("seth", "bannon"),
    ("suzanne", "shaheen"):                ("suzy", "shaheen"),
    ("victoria", "hooker"):               ("vic", "hooker"),
    ("wiz", "khuzai"):                     ("wiz", "abdulla"),
    ("zuzana", "krejciova-rajaniemi"):     ("zuzana", "krejciova"),
    ("diana", "klatt"):                    ("diana", "klatt"),
    ("hope angel", "williams"):            ("hope", "angel williams"),
    ("elien blue", "becque"):              ("elien", "becque"),
    ("isaac", "clark"):                    ("isaac", "clark"),
    ("claire", "bostrom"):                 ("claire", "bostrom"),
    ("sara", "dutson"):                    ("sara", "dutson"),
}

# Master list name -> canonical name (for mismatched names)
MASTER_NAME_MAP = {
    ("fish", "galano friedman"):           ("lisa galano", "friedman"),
    ("lisa", "galano friedman"):           ("lisa galano", "friedman"),
    ("liz", "sia"):                        ("elizabeth", "sia"),
}


def derive_status(thu, fri, wedding, sunday):
    """Derive an overall RSVP status from per-event responses."""
    events = [thu, fri, wedding, sunday]
    if any(e == "Attending" for e in events):
        if all(e in ("Declined", "No Response") for e in events if e != "Attending"):
            return "Attending"
        return "Attending"
    if all(e == "Declined" for e in events):
        return "Declined"
    if all(e == "No Response" for e in events):
        return "No Response"
    if any(e == "Declined" for e in events):
        return "Mixed/Incomplete"
    return "No Response"


def clean_guest_row(first, last):
    """Clean and validate a guest row. Returns (first, last) or None to skip."""
    if not first and not last:
        return None
    # Skip unnamed plus-one placeholders
    if first.lower() == "guest" and not last:
        return None
    # Fix names where full name ended up in first_name with empty last_name
    if " " in first and not last:
        parts = first.rsplit(" ", 1)
        first, last = parts[0], parts[1]
    return first, last


def read_export():
    """Read export.csv — the most up-to-date guest list & RSVPs."""
    guests = []
    with open(EXPORT_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            first = row.get("First Name", "").strip()
            last = row.get("Last Name", "").strip()
            cleaned = clean_guest_row(first, last)
            if not cleaned:
                continue
            first, last = cleaned

            thu = row.get("Thursday Welcome Dinner & Cocktails", "").strip()
            fri = row.get("Friday Welcome Dinner & Cocktails", "").strip()
            wedding = row.get("Wedding Ceremony & Party", "").strip()
            sunday = row.get("Come Down Dinner", "").strip()

            guests.append({
                "first_name": first,
                "last_name": last,
                "full_name": f"{first} {last}",
                "title": row.get("Title", "").strip(),
                "suffix": row.get("Suffix", "").strip(),
                "rsvp_thursday": thu,
                "rsvp_friday": fri,
                "rsvp_wedding": wedding,
                "rsvp_sunday": sunday,
                "rsvp_status": derive_status(thu, fri, wedding, sunday),
            })
    return guests


def read_full_list():
    """Read full_list.csv for supplementary data (Status, Plus One)."""
    extras = {}
    if not os.path.exists(FULL_LIST_CSV):
        return extras
    with open(FULL_LIST_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            first = row.get("First Name", "").strip()
            last = row.get("Last Name", "").strip()
            cleaned = clean_guest_row(first, last)
            if not cleaned:
                continue
            first, last = cleaned
            key = (normalize(first), normalize(last))
            extras[key] = {
                "is_plus_one": row.get("Plus One?", "").strip().upper() == "YES",
            }
    return extras


def read_master():
    """Read master list for contact details and metadata."""
    master = {}
    with open(MASTER_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            first = row.get("FirstName", "").strip()
            last = row.get("Last Name", "").strip()
            if not first and not last:
                continue
            key = (normalize(first), normalize(last))
            key = MASTER_NAME_MAP.get(key, key)
            master[key] = {
                "email": row.get("Email", "").strip(),
                "group_house": row.get("Group House?", "").strip(),
                "guest_name": row.get("Guest Name", "").strip(),
                "address": row.get("Address", "").strip(),
                "phone": row.get("Phone", "").strip(),
                "person": row.get("Person", "").strip(),
                "food_choice_raw": row.get("Food Choice", "").strip(),
                "main_first": first,
                "main_last": last,
            }
    return master


KNOWN_FOODS = {"fish": "Fish", "kebab": "Kebab", "chille": "Chille"}


def normalize_food(f):
    f = f.strip()
    return KNOWN_FOODS.get(f.lower(), f)


def split_food_and_note(token):
    """Split 'Fish NO TOMATO' -> ('Fish', 'NO TOMATO'). Returns (food, note_or_None)."""
    m = re.match(r"^([A-Za-z]+)\s+(NO\s+.+)$", token.strip(), re.IGNORECASE)
    if m:
        return normalize_food(m.group(1)), m.group(2).strip()
    return normalize_food(token), None


def parse_food_spec(raw):
    """
    Parse a Food Choice cell.

    Returns a tuple:
      (main_food, main_note, guest_food, guest_note, kids_foods, labeled_by_name)

    - main_food/guest_food: (food, note) or (None, None)
    - kids_foods: list of (food, note) for children, in order (empty if 0 or 1-2 foods)
    - labeled_by_name: {first_name_lower: (food, note)} for explicit labeled form
    """
    raw = (raw or "").strip()
    if not raw:
        return (None, None, None, None, [], {})

    # Labeled form: "Chille (Neha) Kebab (James)"
    labeled = re.findall(r"([A-Za-z]+)\s*\(([^)]+)\)", raw)
    if labeled and len(labeled) >= 2:
        mapping = {}
        for food, name in labeled:
            mapping[name.strip().lower()] = (normalize_food(food), None)
        return (None, None, None, None, [], mapping)

    if "," not in raw:
        food, note = split_food_and_note(raw)
        return (food, note, None, None, [], {})

    parts = [p.strip() for p in raw.split(",") if p.strip()]
    parsed = [split_food_and_note(p) for p in parts]
    main = parsed[0] if len(parsed) >= 1 else (None, None)
    guest = parsed[1] if len(parsed) >= 2 else (None, None)
    kids = parsed[2:] if len(parsed) > 2 else []
    return (main[0], main[1], guest[0], guest[1], kids, {})


def split_full_name(full):
    full = (full or "").strip()
    if not full:
        return "", ""
    parts = full.rsplit(" ", 1)
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


def compute_food_assignments(export_guests, master):
    """
    Returns {(first_norm, last_norm): (food, note)} plus a list of warnings.
    """
    assignments = {}
    warnings = []

    # Build set of keys that are a "main" or "guest" in master (to exclude from kid matching)
    master_main_keys = set(master.keys())
    master_guest_keys = set()
    for mkey, m in master.items():
        gfirst, glast = split_full_name(m["guest_name"])
        if gfirst or glast:
            master_guest_keys.add((normalize(gfirst), normalize(glast)))

    reserved = master_main_keys | master_guest_keys

    for mkey, m in master.items():
        raw = m["food_choice_raw"]
        if not raw:
            continue
        main_first = m["main_first"]
        main_last = m["main_last"]
        guest_first, guest_last = split_full_name(m["guest_name"])

        main_food, main_note, guest_food, guest_note, kids, labeled = parse_food_spec(raw)

        if labeled:
            # Match by first name across main and guest
            for name_lower, (food, note) in labeled.items():
                if normalize(main_first) == name_lower:
                    assignments[mkey] = (food, note)
                elif guest_first and normalize(guest_first) == name_lower:
                    assignments[(normalize(guest_first), normalize(guest_last))] = (food, note)
                else:
                    warnings.append(
                        f"Labeled food name {name_lower!r} did not match "
                        f"main ({main_first}) or guest ({guest_first}) for row {main_first} {main_last}"
                    )
            continue

        if main_food is not None:
            assignments[mkey] = (main_food, main_note)
        if guest_food is not None and (guest_first or guest_last):
            assignments[(normalize(guest_first), normalize(guest_last))] = (guest_food, guest_note)

        # Kids: scan export.csv rows for last-name matches
        if kids:
            wanted_lasts = {normalize(main_last)}
            if guest_last:
                wanted_lasts.add(normalize(guest_last))

            matched = []
            for g in export_guests:
                gk = (normalize(g["first_name"]), normalize(g["last_name"]))
                if gk in reserved:
                    continue
                if g.get("rsvp_wedding") != "Attending":
                    continue
                if normalize(g["last_name"]) in wanted_lasts:
                    matched.append(gk)
                    if len(matched) == len(kids):
                        break

            if len(matched) < len(kids):
                warnings.append(
                    f"{main_first} {main_last}: expected {len(kids)} kid(s) by last-name "
                    f"match but found {len(matched)} (foods: {[k[0] for k in kids]})"
                )
            for kid_key, (food, note) in zip(matched, kids):
                assignments[kid_key] = (food, note)

    return assignments, warnings


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
    """Read form responses (how they know the couple, photo, etc.).

    The Google Form's question text gets edited periodically (e.g.
    adding "Please answer in detail"), so we match column headers by
    their stable opening phrase rather than exact full text.
    """
    responses = {}
    with open(FORM_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            first = row.get("First Name", "").strip()
            last = row.get("Last Name", "").strip()
            key = (normalize(first), normalize(last))
            key = FORM_NAME_MAP.get(key, key)
            responses[key] = {
                "how_we_know":    _first_matching_column(row, "How do you know"),
                "photo_url":      _first_matching_column(row, "Share a photo", "Please Upload a Photo", "Photo URL"),
                # Hilary & Elliot's form asks for a go-to karaoke song instead of a
                # "least favorite thing about weddings". We carry it in the existing
                # least_favorite column; the profile label reads as karaoke (build.py).
                "least_favorite": _first_matching_column(row, "What is one of your go-to karaoke", "*Bonus* Life is Editing", "Least Favorite"),
                "pronouns":       _first_matching_column(row, "Pronoun"),
                "form_current_city": _first_matching_column(row, "What city or town do you live in now"),
                "form_hometown":     _first_matching_column(row, "Where did you grow up"),
                "form_memory":       _first_matching_column(row, "If you'd like share a memory", "If you'd like to share a memory"),
            }
    return responses


def merge_and_write(export_guests, full_list_extras, master, form_responses):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    food_assignments, food_warnings = compute_food_assignments(export_guests, master)
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

    master_matched = 0
    form_matched = 0
    unmatched_form = set(form_responses.keys())

    for g in export_guests:
        key = (normalize(g["first_name"]), normalize(g["last_name"]))

        # Look up supplementary data from full_list
        fl = full_list_extras.get(key, {})

        # Look up master list data
        m = master.get(key, {})
        if m:
            master_matched += 1

        # Look up form response
        form = form_responses.get(key, {})
        if form:
            form_matched += 1
            unmatched_form.discard(key)

        initials = ""
        if g["last_name"] and g["first_name"]:
            initials = g["last_name"][0].upper() + g["first_name"][0].upper()

        food_choice, food_note = food_assignments.get(key, (None, None))

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
            g["first_name"], g["last_name"], g["full_name"],
            g["title"], g["suffix"],
            m.get("email", ""), m.get("group_house", ""),
            m.get("guest_name", ""), m.get("address", ""),
            m.get("phone", ""), m.get("person", ""),
            food_choice or "", food_note or "",
            g["rsvp_status"], g["rsvp_thursday"], g["rsvp_friday"],
            g["rsvp_wedding"], g["rsvp_sunday"],
            1 if fl.get("is_plus_one", False) else 0,
            form.get("how_we_know", ""),
            resolve_photo(g["first_name"], g["last_name"],
                          form.get("photo_url", ""), local_photos),
            form.get("least_favorite", ""),
            form.get("form_current_city", ""),
            form.get("form_hometown", ""),
            form.get("form_memory", ""),
            form.get("pronouns", ""),
            initials,
        ))

    # Form respondents not in the export list — add them as attending
    for key in list(unmatched_form):
        form = form_responses[key]
        first, last = key
        first_cap = first.title()
        last_cap = last.title()

        initials = ""
        if last_cap and first_cap:
            initials = last_cap[0].upper() + first_cap[0].upper()

        m = master.get(key, {})
        food_choice, food_note = food_assignments.get(key, (None, None))

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
            first_cap, last_cap, f"{first_cap} {last_cap}", "", "",
            m.get("email", ""), m.get("group_house", ""),
            m.get("guest_name", ""), m.get("address", ""),
            m.get("phone", ""), m.get("person", ""),
            food_choice or "", food_note or "",
            "Attending", "", "", "", "",
            0,
            form.get("how_we_know", ""),
            resolve_photo(first_cap, last_cap,
                          form.get("photo_url", ""), local_photos),
            form.get("least_favorite", ""),
            form.get("form_current_city", ""),
            form.get("form_hometown", ""),
            form.get("form_memory", ""),
            form.get("pronouns", ""),
            initials,
        ))
        form_matched += 1
        unmatched_form.discard(key)

    conn.commit()

    c.execute("SELECT COUNT(*) FROM guests")
    total = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM guests WHERE rsvp_status = 'Attending'")
    attending = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM guests WHERE how_we_know != ''")
    with_form = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM guests WHERE rsvp_status = 'Declined'")
    declined = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM guests WHERE rsvp_status = 'No Response'")
    no_resp = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM guests WHERE rsvp_status = 'Mixed/Incomplete'")
    mixed = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM guests WHERE food_choice != ''")
    food_count = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM guests WHERE photo_url LIKE 'images/%'")
    local_photo_count = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM guests WHERE form_current_city != ''")
    form_city_count = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM guests WHERE form_hometown != ''")
    form_hometown_count = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM guests WHERE form_memory != ''")
    form_memory_count = c.fetchone()[0]

    conn.close()

    print(f"Merged {total} guests into wedding.db")
    print(f"  {attending} attending, {declined} declined, {no_resp} no response, {mixed} mixed")
    print(f"  {master_matched} matched to master list (contact details)")
    print(f"  {with_form} have form responses")
    print(f"  {form_matched} form responses matched")
    if unmatched_form:
        print(f"  {len(unmatched_form)} form responses with no match:")
        for first, last in sorted(unmatched_form):
            print(f"    - {first} {last}")

    print(f"  {food_count} have food_choice assigned")
    print(f"  {local_photo_count} have local photos")
    print(f"  form fields: {form_city_count} current_city, "
          f"{form_hometown_count} hometown, {form_memory_count} memory")
    if food_warnings:
        print(f"  Food-parsing warnings ({len(food_warnings)}):")
        for w in food_warnings:
            print(f"    - {w}")


# ── Contact-form ingestion ──────────────────────────────────────────────
# Reads contact_form_responses.csv (post-wedding contact info: phone,
# email, Instagram, LinkedIn, Twitter, BlueSky, Soundcloud) and writes
# matched rows into wedding.db's `guest_contacts` table.
#
# Free-text form fields are messy in the wild — handles vs. @handles
# vs. full URLs with tracking params, phone formatting variants, "N/A"
# entries. The normalize_* helpers below canonicalize on ingest so the
# rendering layer can assume clean values.

# Maps a normalized full name (lowercased, single-spaced) to a known
# (first_norm, last_norm) guest key. Used when fuzzy splitting the
# contact form's single "Full Name" field can't reach an existing guest
# row on its own — e.g. married-name drift, typos, first-name-only
# submissions. Keep entries here narrow and verified by Elien/Nima; do
# NOT auto-add by inference.
CONTACT_FORM_NAME_MAP = {
    "asma amani":                  ("asma", "ahmed"),
    "zuzana krejciova-rajaniemi":  ("zuzana", "krejciova"),
    "sep":                          ("sep", "norouzi"),
    "alexa meywr":                  ("alexandra", "meyer"),
    "lena elkousy":                 ("lena", "elsouky"),
    "barbara soalheiro gwercman":   ("barbara", "gwercman"),
    "hope angel williams":          ("hope", "angel williams"),
}


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
        - '/in/foo' (path-only, no host) — seen in Anna Spisak's submission
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
    name. Tries every plausible split so a name like 'Barbara Soalheiro
    Gwercman' can reach both (barbara, soalheiro gwercman) and (barbara
    soalheiro, gwercman) — whichever the guest table has."""
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
    #    aliases (married-name drift etc.).
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
    for path, label in [
        (EXPORT_CSV, "export.csv"),
        (MASTER_CSV, "master_list.csv"),
        (FORM_CSV, "form_responses.csv"),
    ]:
        if not os.path.exists(path):
            print(f"Missing {label}")
            return

    export_guests = read_export()
    full_list_extras = read_full_list()
    master = read_master()
    form_responses = read_form_responses()
    merge_and_write(export_guests, full_list_extras, master, form_responses)

    # Contact form is optional — skip silently if the CSV isn't there.
    # This is how Nima's sync routine treats the original form CSV too:
    # download if available, run merge regardless.
    if os.path.exists(CONTACT_FORM_CSV):
        contact_rows = read_contact_form_responses()
        merge_contacts(contact_rows)
    else:
        print("Contacts: contact_form_responses.csv not present, skipping")


if __name__ == "__main__":
    main()
