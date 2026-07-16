#!/usr/bin/env python3
"""
Local seating-chart editor backed by wedding.db.

Usage:
    python3 scripts/edit_seating.py

Opens http://localhost:8766/seating in your browser. Reads the
attending guest list from wedding.db's `guests` table and persists
all seating decisions (tags, table layouts, seat assignments,
per-guest seating-chart metadata) into wedding.db.

The seating chart's source of truth is wedding.db. This server
hands the front-end a normalized view of state on GET /api/state
and writes back the whole shape on POST /api/state.

Mirror of scripts/edit_guests.py, but focused on seating only.
Different port (8766) so both editors can run in parallel.

On first run, the migration step seeds wedding.db with the same
defaults + pre-assignments that the original stand-alone seating
chart shipped with (Casa Hyder Thursday/Friday layouts, Instituto
Saturday layout, the three-day pre-assignment that lived in the
"draft" HTML files). After that the DB is authoritative.
"""
from __future__ import annotations

import http.server
import json
import os
import sqlite3
import threading
import webbrowser
from typing import Any

DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(DIR, "wedding.db")
HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "seating_app.html")
PORT = 8766


# ── Schema ─────────────────────────────────────────────────────────────────

SEATING_SCHEMA = """
CREATE TABLE IF NOT EXISTS seating_tags (
    tag_key    TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    color      TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS seating_tables (
    day         TEXT NOT NULL CHECK (day IN ('thu','fri','sat')),
    table_key   TEXT NOT NULL,
    table_num   INTEGER NOT NULL,
    name        TEXT NOT NULL DEFAULT '',
    capacity    INTEGER NOT NULL,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    layout_json TEXT,
    PRIMARY KEY (day, table_key)
);

CREATE TABLE IF NOT EXISTS seating_assignments (
    day        TEXT NOT NULL,
    table_key  TEXT NOT NULL,
    seat_idx   INTEGER NOT NULL,
    guest_id   INTEGER NOT NULL,
    PRIMARY KEY (day, table_key, seat_idx)
);

CREATE INDEX IF NOT EXISTS ix_seating_assignments_guest
    ON seating_assignments(guest_id);

CREATE TABLE IF NOT EXISTS seating_guest_meta (
    guest_id  INTEGER PRIMARY KEY,
    tag_key   TEXT,
    is_kid    INTEGER NOT NULL DEFAULT 0,
    note      TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS seating_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
"""


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA foreign_keys = ON")
    return con


def ensure_schema() -> None:
    with _conn() as con:
        con.executescript(SEATING_SCHEMA)


# ── Defaults (mirrors the original stand-alone HTML) ───────────────────────

DEFAULT_TAGS = [
    ("bride_family", "Bride's family", "#c85a5a", 0),
    ("groom_family", "Groom's family", "#d48a3e", 1),
    ("friends",      "Friends",         "#5a8fc8", 2),
    ("work",         "Work",            "#7a5ac8", 3),
    ("kids",         "Kids",            "#5ac887", 4),
]


def _default_casa_tables(day: str) -> list[dict[str, Any]]:
    """Casa Hyder default layout — Thursday (11 + kids), Friday (12 + kids)."""
    if day == "thu":
        order = [11, 9, 10, 7, 8, 5, 6, 3, 4, 1, 2]
        cap_for = lambda n: 10  # noqa: E731
    elif day == "fri":
        order = [11, 12, 9, 10, 7, 8, 5, 6, 3, 4, 1, 2]
        cap_for = lambda n: 12 if n == 12 else 10  # noqa: E731
    else:
        raise ValueError(day)

    tables: list[dict[str, Any]] = []
    for sort_idx, num in enumerate(order):
        tables.append({
            "table_key":   f"{day}_{num}",
            "table_num":   num,
            "name":        "",
            "capacity":    cap_for(num),
            "sort_order":  sort_idx,
            "layout_json": None,
        })
    tables.append({
        "table_key":   f"{day}_13",
        "table_num":   13,
        "name":        "Kids Table",
        "capacity":    8,
        "sort_order":  len(tables),
        "layout_json": json.dumps({"zone": "kids"}),
    })
    return tables


def _default_instituto_tables() -> list[dict[str, Any]]:
    """Saturday at Instituto: 20 tables matching Draft Layout PDF."""
    base = [
        (20, "t20", 10, False),
        (19, "t19", 10, False),
        (18, "t18", 10, False),
        (17, "t17", 10, False),
        (14, "t14", 10, False),
        (15, "t15", 10, False),
        (16, "t16", 10, False),
        (13, "t13", 10, False),
        (12, "t12", 10, False),
        (11, "t11", 10, False),
        (10, "t10", 10, False),
        (9,  "t9",  10, False),
        (8,  "t8",  10, False),
        (7,  "t7",  10, False),
        (4,  "t4",  12, True),
        (5,  "t5",  10, False),
        (6,  "t6",  10, False),
        (3,  "t3",  12, True),
        (1,  "t1",  10, False),
        (2,  "t2",  10, False),
    ]
    return [{
        "table_key":   f"sat_{num}",
        "table_num":   num,
        "name":        "",
        "capacity":    cap,
        "sort_order":  i,
        "layout_json": json.dumps({"area": area, "long": is_long}),
    } for i, (num, area, cap, is_long) in enumerate(base)]


# Pre-assignments lifted verbatim from the three "draft" HTML files
# (THURSDAY_PRE_ASSIGN, FRIDAY_PRE_ASSIGN, SATURDAY_PRE_ASSIGN). Values
# are the seating-chart's own internal IDs (1..208), which get
# translated to wedding.db guest IDs at migration time. `None` is an
# empty seat (preserved so adjacent guests stay anchored to their
# expected seat numbers).
THURSDAY_PRE_ASSIGN = {
    1:  [5, 6, 39, 38, 82, 62, 40, 41, 30, 152],
    2:  [63, 81, 189, 190, 105, 106, 146, 147, 47, 155],
    3:  [17, 165, 12, 67, 68, None, 192, None, 108, 191],
    4:  [29, 193, 137, 141, 179, 20, 28, 161, 69, 139],
    5:  [52, 167, 176, 119, 59, 34, 54, 130, 71, 70],
    6:  [55, 96, 26, 27, 208, 49, 57, 58, 88, 87],
    7:  [115, 11, 65, 136, 135, 53, 116, 200, 118, 117],
    8:  [168, 101, 7, 151, 195, 91, 13, None, 172, 107],
    9:  [25, 24, 78, 205, 76, 51, 112, 113, None, 50],
    10: [66, 19, 56, 23, 80, 201, 140, 10, 97, 114],
    11: [73, 104, 74, 75, 18, 171, 77, 175, 60, 173],
    13: [42, 43, 64, 153],
}

FRIDAY_PRE_ASSIGN = {
    1:  [149, 148, 164, 163, 207, 206, 92, 139, 55, 186],
    2:  [1, 2, 84, 85, 150, 33, 93, 140, 168, 191],
    3:  [3, 4, 89, 90, 8, 35, 98, 143, 169, 192],
    4:  [145, 144, 99, 100, 9, 36, 101, 142, 170, 194],
    5:  [28, 29, 102, 103, 10, 37, 108, 154, 174, 195],
    6:  [95, 94, 121, 122, 16, 48, 109, 156, 46, 196],
    7:  [185, 184, 126, 125, 20, 60, 110, 157, 177, 197],
    8:  [129, 128, 199, 198, 21, 61, 111, 158, 178, 202],
    9:  [73, 104, 105, 106, 22, 72, 120, 159, 180, 173],
    10: [19, 66, 18, 15, 14, 79, 127, 208, 181, 205],
    11: [74, 76, 77, 78, 31, 83, 131, 162, 182],
    12: [75, 17, 204, 203, 32, 91, 138, 166, 183],
    13: [86, 123, 124, 132, 133, 134],
}

SATURDAY_PRE_ASSIGN = {
    1:  [149, 148, 57, 58, 9, 35, 80, 119, 175, 178],
    2:  [1, 2, 62, 63, 12, 36, 83, 120, 158, 180],
    3:  [73, 104, 105, 106, 76, 77, 78, 127, 159, 43],
    4:  [18, 19, 17, 7, 13, 37, 86, 131, 162, 42],
    5:  [3, 4, 70, 71, 134, 47, 91, 135, 165, 181],
    6:  [5, 6, 164, 163, 133, 48, 92, 136, 166, 182],
    7:  [10, 11, 81, 82, 16, 52, 93, 137, 167, 25],
    8:  [145, 144, 84, 85, 20, 53, 96, 138, 55, 183],
    9:  [27, 26, 87, 88, 21, 54, 97, 139, 168, 186],
    10: [147, 146, 89, 90, 22, 56, 98, 140, 124, 189],
    11: [75, 74, 99, 100, 132, 59, 101, 141, 123, 191],
    12: [39, 38, 102, 103, 23, 60, 107, 143, 118, 192],
    13: [28, 29, 121, 122, 24, 61, 108, 142, 169, 193],
    14: [30, 152, 126, 125, 153, 64, 109, 151, 170, 194],
    15: [40, 41, 199, 198, 31, 65, 110, 154, 171, 195],
    16: [95, 94, 204, 203, 32, 66, 111, 155, 172, 196],
    17: [185, 184, 200, 201, 33, 67, 112, 116, 174, 197],
    18: [49, 208, 15, 14, 34, 69, 114, 156, 46, 202],
    19: [50, 51, 207, 206, 68, 72, 115, 157, 176, 173],
    20: [129, 128, 150, 8, 113, 79, 117, 190, 177, 205],
}

# Kid flags carried in the original [id, first, last, thu, fri, sat, isKid]
# rows. Encoded here as the seating-chart's internal IDs to keep the
# migration self-contained.
KID_SC_IDS = {42, 43, 64, 86, 123, 124, 132, 133, 134, 153}

# Seating-chart IDs 1..208 → (first_name, last_name) so we can look up
# each one in wedding.db at migration time. Frozen verbatim from the
# `GUEST_DATA` array that lived in the stand-alone HTML.
SC_GUEST_NAMES: list[tuple[int, str, str]] = [
    (1, "Wiz", "Abdulla"), (2, "Yael", "Hendel"), (3, "Maryam", "Aghdaee"),
    (4, "Masoud", "Zeighami"), (5, "Asma", "Ahmed"), (6, "Niles", "Murphy"),
    (7, "Inaya", "Ahmed"), (8, "Menooa", "Akbari"), (9, "Esra", "Al"),
    (10, "Eric", "Anderson"), (11, "Raven", "Anderson"), (12, "David", "Aron"),
    (13, "Jacob", "Azia"), (14, "Fahim", "Aziz"), (15, "Hope", "Angel Williams"),
    (16, "Seth", "Bannon"), (17, "Averi", "Becque"), (18, "Bruce", "Becque"),
    (19, "Suzanne", "Becque"), (20, "Olivia", "Benjamin"),
    (21, "Kasia", "Bojanowska"), (22, "Stella", "Bongiorno"),
    (23, "Daniel", "Bunker"), (24, "Mason", "Bunker"), (25, "Ben", "Schmandt"),
    (26, "Christine", "Busaba"), (27, "Joe", "Bejany"),
    (28, "Jon", "Carr-Harris"), (29, "Natalie", "Wei"),
    (30, "Lance", "Cassidy"), (31, "Jose", "Cavazos"),
    (32, "Rodrigo", "Cetina-Presuel"), (33, "Simbarashe", "Cha"),
    (34, "Jesse", "Chand"), (35, "Monica", "Coffey"), (36, "Joseph", "Cohen"),
    (37, "Alex", "Colby"), (38, "Kia", "Dalili"), (39, "Jenn", "Breslow"),
    (40, "Neda", "Dalili"), (41, "Ryan", "Saunders"),
    (42, "Leila", "Saunders"), (43, "Ava", "Saunders"),
    (44, "Francis", "Davidson"), (45, "Maria", "Davidson"),
    (46, "Floor", "de Ruijter"), (47, "Kate", "Deibler"),
    (48, "Ashu", "Desai"), (49, "Greg", "Dingle"), (50, "Betty", "Dittrich"),
    (51, "Charlie", "Dittrich"), (52, "Katrina", "Dittrich"),
    (53, "Matt", "Doyle"), (54, "Grace", "Edinger"),
    (55, "Karim", "El Rabiey"), (56, "Deborah", "Elman"),
    (57, "Megan", "Elsayed"), (58, "Omar", "Elsayed"),
    (59, "Lena", "Elsouky"), (60, "Maggie", "Falter"),
    (61, "Nina", "Faulhaber"), (62, "Reed", "Finlay"),
    (63, "Milana", "Vayntrub"), (64, "Ever", "Finlay"),
    (65, "Nicole", "Fish"), (66, "Richard", "Fisher"),
    (67, "Lane", "Florsheim"), (68, "David", "Cho"),
    (69, "Alex", "Fraenkel"), (70, "Ian", "Friedman"),
    (71, "Lisa Galano", "Friedman"), (72, "Marika", "Frumes"),
    (73, "Iraj", "Gardideh"), (74, "Nima", "Gardideh"),
    (75, "Elien", "Becque"), (76, "Peyman", "Gardideh"),
    (77, "Turaj", "Gardideh"), (78, "Dunja", "Gardideh"),
    (79, "Viksit", "Gaur"), (80, "Ayesha", "Ghosh"),
    (81, "Larry", "Gilman"), (82, "Priscilla", "Gilman"),
    (83, "Asad", "Goodarzy"), (84, "Yoshio", "Goto"),
    (85, "Elizabeth", "Sia"), (86, "Takezo", "Goto"),
    (87, "Barbara", "Gwercman"), (88, "Sergio", "Gwercman"),
    (89, "Dino", "Hodzik"), (90, "Blanka", "Nakova"),
    (91, "Gabi", "Holzwarth"), (92, "Jane", "Hong"),
    (93, "Vic", "Hooker"), (94, "Brenda", "Hsueh"),
    (95, "Cyrus", "Deboo"), (96, "Sanaria", "Hussain"),
    (97, "Anne", "Hutchins"), (98, "Oka", "Hutchins"),
    (99, "Johnny", "Hwin"), (100, "Diana", "Klatt"),
    (101, "Jordana", "Jacobs"), (102, "Archie", "Japaridze"),
    (103, "Anna", "Pickren"), (104, "Elham", "Jeddi"),
    (105, "Rokhsareh", "Jeddi"), (106, "Saeed", "Shafazand"),
    (107, "Josh", "Katz"), (108, "Gayatri", "Kawlra"),
    (109, "Sam", "Keene"), (110, "Selin", "Kent"),
    (111, "Patrik", "Khach"), (112, "Easton", "Kirschner"),
    (113, "Isaac", "Clark"), (114, "Jim", "Kirschner"),
    (115, "Jonathan", "Kirschner"), (116, "Gabrielle", "Muntean"),
    (117, "Zuzana", "Krejciova"), (118, "Hannu", "Rajaniemi"),
    (119, "Alon", "Krifcher"), (120, "Lindsay", "Kruit"),
    (121, "Marjan", "Kusha"), (122, "Mehdi", "Rafiei"),
    (123, "Dion", "Rafiei"), (124, "Darian", "Rafiei"),
    (125, "Elyse", "Lefebvre"), (126, "Alex", "Lamb"),
    (127, "Katya", "Levy"), (128, "Andrew", "Look"),
    (129, "Sara", "Dutson"), (130, "Hilary", "Ludlow"),
    (131, "Ela", "Madej"), (132, "Amelie", "Bricker"),
    (133, "Ray", "Bannon"), (134, "Aiko", "Bannon"),
    (135, "Axel", "Mansoor"), (136, "Souki", "Mansoor"),
    (137, "Steve", "Martocci"), (138, "Maggi", "McCaw"),
    (139, "Brad", "Menezes"), (140, "Alexandra", "Meyer"),
    (141, "Kelly", "Monroe"), (142, "Mahshid", "Moradin"),
    (143, "Jacob", "Moradin"), (144, "Neda", "Moradin"),
    (145, "Hooman", "Bandarchi"), (146, "Drew", "Moxon"),
    (147, "Claire", "Bostrom"), (148, "Elham", "Mozaffary"),
    (149, "Kamran", "Abbasiyan"), (150, "Shaana", "Abbasiyan"),
    (151, "Parker", "Muir"), (152, "Maisa", "Mumtaz-Cassidy"),
    (153, "Zhen", "Cassidy"), (154, "Ryan", "Munch"),
    (155, "Azim", "Munivar"), (156, "Berke", "Nayman"),
    (157, "Rishika", "Negi"), (158, "Sep", "Norouzi"),
    (159, "Philipp", "Ojha"), (160, "Madelaine", "Ojha"),
    (161, "Ryan", "Pandya"), (162, "Mark", "Pieri"),
    (163, "Katie", "Porter"), (164, "John", "Gillis"),
    (165, "Jake", "Poses"), (166, "Chloe", "Powell"),
    (167, "Justin", "Pratt"), (168, "Josh", "Radnor"),
    (169, "Quetza", "Ramirez"), (170, "Drew", "Remiker"),
    (171, "Sara", "Rodell"), (172, "Carli", "Roth"),
    (173, "Mary", "Royall Wilgis"), (174, "Rachel", "Ruback"),
    (175, "Ashrafolsadat", "Safavi Nia"), (176, "Alexandra", "Saltiel"),
    (177, "Maged", "Sami"), (178, "Elinor", "Samuelsson"),
    (179, "Jen", "Sanduski"), (180, "Peter", "Sauer"),
    (181, "Elena", "Saurel"), (182, "Nina", "Scheinman"),
    (183, "Cosmin", "Serban"), (184, "Alap", "Shah"),
    (185, "Dessa", "Delfin"), (186, "Suzy", "Shaheen"),
    (187, "Mastaneh", "Shahlaei"), (188, "Amir", "Mohammadi"),
    (189, "Neha", "Shastry"), (190, "James", "Nelson"),
    (191, "Pranjal", "Singh"), (192, "Anna", "Spisak"),
    (193, "Anna", "Stock-Matthews"), (194, "Igor", "Terzic"),
    (195, "Will", "Thomas"), (196, "Sonia", "Tsao"),
    (197, "Chris", "Waclawek"), (198, "Qiming", "Weng"),
    (199, "Joanna", "Man"), (200, "Christina", "White"),
    (201, "Zac", "White"), (202, "Billie", "Whitehouse"),
    (203, "Lori", "Ying"), (204, "Thomas", "McCormick"),
    (205, "Jess", "Yu"), (206, "Samira", "Zargarzadah"),
    (207, "Jawad", "Zadeh"), (208, "Alys", "Ollier"),
]


def _build_sc_to_db_map(con: sqlite3.Connection) -> dict[int, int]:
    """Map seating-chart guest IDs (1..208) → wedding.db guest IDs by name."""
    rows = con.execute(
        "SELECT id, first_name, last_name FROM guests"
    ).fetchall()

    by_name: dict[tuple[str, str], list[int]] = {}
    for gid, first, last in rows:
        key = ((first or "").strip().lower(), (last or "").strip().lower())
        by_name.setdefault(key, []).append(gid)

    mapping: dict[int, int] = {}
    missing: list[tuple[int, str, str]] = []
    for sc_id, first, last in SC_GUEST_NAMES:
        key = (first.lower(), last.lower())
        candidates = by_name.get(key, [])
        if not candidates:
            missing.append((sc_id, first, last))
            continue
        # Prefer the lowest id (the canonical guests-table row); duplicate
        # names (e.g. two Oren Hods) are extremely rare and excluded from
        # the seating chart's attending list anyway.
        mapping[sc_id] = min(candidates)

    if missing:
        details = ", ".join(f"{f} {l} (sc#{i})" for i, f, l in missing)
        raise SystemExit(
            "Could not map seating-chart guest IDs to wedding.db: " + details
        )
    return mapping


# ── One-time migration ─────────────────────────────────────────────────────

MIGRATION_KEY = "initial_migration_v1"


def _migrate_initial(con: sqlite3.Connection) -> None:
    """Seed defaults + apply the three-day pre-assignment from the original
    stand-alone seating chart. Idempotent — runs once, marked via
    seating_meta. Safe to call on every server start."""
    cur = con.cursor()
    done = cur.execute(
        "SELECT value FROM seating_meta WHERE key = ?", (MIGRATION_KEY,)
    ).fetchone()
    if done:
        return

    # If a prior partial run left rows behind we'd double-seed. Wipe the
    # seating_* tables (NOT the marker table itself) so this start is clean.
    # In practice this only runs on a fresh DB.
    cur.executescript("""
        DELETE FROM seating_assignments;
        DELETE FROM seating_tables;
        DELETE FROM seating_tags;
        DELETE FROM seating_guest_meta;
    """)

    # 1. Tags
    cur.executemany(
        "INSERT INTO seating_tags (tag_key, name, color, sort_order) "
        "VALUES (?, ?, ?, ?)",
        DEFAULT_TAGS,
    )

    # 2. Tables (default layouts)
    for day in ("thu", "fri"):
        for t in _default_casa_tables(day):
            cur.execute(
                "INSERT INTO seating_tables "
                "(day, table_key, table_num, name, capacity, sort_order, layout_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (day, t["table_key"], t["table_num"], t["name"],
                 t["capacity"], t["sort_order"], t["layout_json"]),
            )
    for t in _default_instituto_tables():
        cur.execute(
            "INSERT INTO seating_tables "
            "(day, table_key, table_num, name, capacity, sort_order, layout_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("sat", t["table_key"], t["table_num"], t["name"],
             t["capacity"], t["sort_order"], t["layout_json"]),
        )

    # 3. Guest ID translation map
    sc_to_db = _build_sc_to_db_map(con)

    # 4. Seat assignments — translate sc_id → db_id, preserve None gaps as
    # missing rows (seat_idx skipped). The front-end pads sparse arrays.
    pre_assigns = {
        "thu": THURSDAY_PRE_ASSIGN,
        "fri": FRIDAY_PRE_ASSIGN,
        "sat": SATURDAY_PRE_ASSIGN,
    }
    for day, by_table in pre_assigns.items():
        for table_num, seats in by_table.items():
            table_key = f"{day}_{table_num}"
            for idx, sc_id in enumerate(seats):
                if sc_id is None:
                    continue
                db_id = sc_to_db.get(sc_id)
                if db_id is None:
                    raise SystemExit(
                        f"sc id {sc_id} not in mapping (day={day} table={table_num} seat={idx})"
                    )
                cur.execute(
                    "INSERT INTO seating_assignments "
                    "(day, table_key, seat_idx, guest_id) VALUES (?, ?, ?, ?)",
                    (day, table_key, idx, db_id),
                )

    # 5. Per-guest meta — kids get the kids tag and is_kid flag
    for sc_id in KID_SC_IDS:
        db_id = sc_to_db.get(sc_id)
        if db_id is None:
            continue
        cur.execute(
            "INSERT OR REPLACE INTO seating_guest_meta "
            "(guest_id, tag_key, is_kid, note) VALUES (?, ?, 1, '')",
            (db_id, "kids"),
        )

    cur.execute(
        "INSERT OR REPLACE INTO seating_meta (key, value) VALUES (?, ?)",
        (MIGRATION_KEY, "1"),
    )
    con.commit()


# ── Load / Save ────────────────────────────────────────────────────────────

DAY_TO_DB_RSVP = {
    "thu": "rsvp_thursday",
    "fri": "rsvp_friday",
    "sat": "rsvp_wedding",
}

# Mirrors build.py's ROMANTIC_LABELS — the subset of relationship labels
# that count as "they came together as a couple". The seating editor uses
# these to flag a seat in light red when its occupant isn't sitting with
# the person they're in a relationship with.
ROMANTIC_LABELS = {
    "spouse", "spouses",
    "wife", "wives",
    "husband", "husbands",
    "partner", "partners",
    "boyfriend", "girlfriend",
    "fiancé", "fiancée", "fiance", "fiancee",
    "date", "dates",
    "plus one", "plus-one", "+1",
    "lover", "lovers",
    "sweetheart", "sweethearts",
    "significant other",
}


def _guest_key(first: str, last: str) -> str:
    return f"{(first or '').strip().lower()}_{(last or '').strip().lower()}".replace(" ", "_")


def load_state() -> dict[str, Any]:
    """Compose the front-end state object from wedding.db."""
    with _conn() as con:
        # Guests — those attending ≥1 of the 3 wedding days, plus anyone
        # who's already been placed in a seat (covers cases like a
        # plus-one whose RSVP is still "No Response" in Zola but who
        # Elien has manually slotted in).
        guests_rows = con.execute("""
            SELECT id, first_name, last_name,
                   rsvp_thursday, rsvp_friday, rsvp_wedding,
                   food_choice
            FROM guests
            WHERE rsvp_thursday = 'Attending'
               OR rsvp_friday   = 'Attending'
               OR rsvp_wedding  = 'Attending'
               OR id IN (SELECT DISTINCT guest_id FROM seating_assignments)
            ORDER BY last_name COLLATE NOCASE, first_name COLLATE NOCASE
        """).fetchall()

        meta_rows = con.execute(
            "SELECT guest_id, tag_key, is_kid, note FROM seating_guest_meta"
        ).fetchall()
        meta_by_guest = {
            gid: (tag_key, bool(is_kid), note or "")
            for gid, tag_key, is_kid, note in meta_rows
        }

        guests = []
        for gid, first, last, rthu, rfri, rsat, food in guests_rows:
            tag_key, is_kid, note = meta_by_guest.get(gid, (None, False, ""))
            guests.append({
                "id":     gid,
                "first":  first or "",
                "last":   last or "",
                "rsvp":   {"thu": rthu or "No Response",
                           "fri": rfri or "No Response",
                           "sat": rsat or "No Response"},
                "isKid":  is_kid,
                "tagId":  tag_key,
                "note":   note,
                "food":   (food or "").strip(),
            })

        tag_rows = con.execute(
            "SELECT tag_key, name, color FROM seating_tags ORDER BY sort_order, tag_key"
        ).fetchall()
        tags = [{"id": k, "name": n, "color": c} for k, n, c in tag_rows]

        # Tables grouped by day, in sort order
        table_rows = con.execute("""
            SELECT day, table_key, table_num, name, capacity, sort_order, layout_json
            FROM seating_tables
            ORDER BY day, sort_order, table_num
        """).fetchall()

        seat_rows = con.execute(
            "SELECT day, table_key, seat_idx, guest_id FROM seating_assignments"
        ).fetchall()

        # Build sparse seat arrays per table
        seats_by_table: dict[tuple[str, str], list[Any]] = {}
        for day, table_key, idx, gid in seat_rows:
            arr = seats_by_table.setdefault((day, table_key), [])
            while len(arr) <= idx:
                arr.append(None)
            arr[idx] = gid

        days: dict[str, dict[str, Any]] = {
            "thu": {"tables": [], "activeTagFilter": None},
            "fri": {"tables": [], "activeTagFilter": None},
            "sat": {"tables": [], "activeTagFilter": None},
        }
        for day, table_key, num, name, cap, _, layout_json in table_rows:
            arr = seats_by_table.get((day, table_key), [])
            # Trim trailing None gaps (front-end does the same on save)
            while arr and arr[-1] is None:
                arr.pop()
            layout = json.loads(layout_json) if layout_json else None
            days[day]["tables"].append({
                "id":       table_key,
                "num":      num,
                "name":     name or "",
                "capacity": cap,
                "seats":    arr,
                "layout":   layout,
            })

        # Romantic-partner pairs, mapped from `relationships` (keyed by
        # guest_key) to the wedding.db numeric guest IDs the front-end
        # uses everywhere else. Only ROMANTIC_LABELS rows count, so
        # family/friend rows in the same table don't trigger the
        # "not sitting with their partner" highlight.
        key_to_id: dict[str, int] = {}
        for gid, first, last in con.execute(
            "SELECT id, first_name, last_name FROM guests"
        ):
            key_to_id[_guest_key(first, last)] = gid

        partners: dict[int, set[int]] = {}
        for a_key, b_key, label in con.execute(
            "SELECT guest_a_key, guest_b_key, label FROM relationships"
        ):
            if (label or "").strip().lower() not in ROMANTIC_LABELS:
                continue
            a_id = key_to_id.get(a_key)
            b_id = key_to_id.get(b_key)
            if a_id is None or b_id is None or a_id == b_id:
                continue
            partners.setdefault(a_id, set()).add(b_id)
            partners.setdefault(b_id, set()).add(a_id)
        partners_out = {gid: sorted(ids) for gid, ids in partners.items()}

        return {
            "guests":     guests,
            "tags":       tags,
            "currentDay": "thu",
            "days":       days,
            "partners":   partners_out,
        }


def save_state(state: dict[str, Any]) -> None:
    """Replace the seating_* tables with the front-end's full state.

    Validates that every guest_id appearing in seats and meta is a real row
    in wedding.db's `guests` table — that's the FK invariant the schema
    relies on. Wraps everything in a single transaction so a bad payload
    never leaves the DB half-written."""
    if not isinstance(state, dict):
        raise ValueError("state must be an object")

    days = state.get("days") or {}
    tags = state.get("tags") or []
    guests = state.get("guests") or []

    for d in ("thu", "fri", "sat"):
        if d not in days:
            raise ValueError(f"missing day: {d}")

    with _conn() as con:
        # Set of valid guest IDs in the main DB for FK validation
        valid_ids = {row[0] for row in con.execute("SELECT id FROM guests")}

        # Validate before we mutate anything
        for d, dayState in days.items():
            for t in (dayState.get("tables") or []):
                for gid in (t.get("seats") or []):
                    if gid is None:
                        continue
                    if not isinstance(gid, int) or gid not in valid_ids:
                        raise ValueError(
                            f"unknown guest_id {gid!r} in {d} {t.get('id')}"
                        )
        for g in guests:
            if not isinstance(g.get("id"), int) or g["id"] not in valid_ids:
                raise ValueError(f"unknown guest_id {g.get('id')!r} in guests array")

        cur = con.cursor()
        cur.execute("BEGIN")
        try:
            cur.execute("DELETE FROM seating_assignments")
            cur.execute("DELETE FROM seating_tables")
            cur.execute("DELETE FROM seating_tags")
            cur.execute("DELETE FROM seating_guest_meta")

            # Tags
            for i, t in enumerate(tags):
                cur.execute(
                    "INSERT INTO seating_tags (tag_key, name, color, sort_order) "
                    "VALUES (?, ?, ?, ?)",
                    (str(t.get("id") or f"tag_{i}"),
                     str(t.get("name") or ""),
                     str(t.get("color") or "#888"),
                     i),
                )

            # Tables + assignments
            for d in ("thu", "fri", "sat"):
                tables = days[d].get("tables") or []
                for sort_idx, t in enumerate(tables):
                    table_key = str(t.get("id") or f"{d}_{t.get('num')}")
                    layout = t.get("layout")
                    layout_json = json.dumps(layout) if layout else None
                    cur.execute(
                        "INSERT INTO seating_tables "
                        "(day, table_key, table_num, name, capacity, sort_order, layout_json) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (d, table_key,
                         int(t.get("num") or 0),
                         str(t.get("name") or ""),
                         int(t.get("capacity") or 10),
                         sort_idx,
                         layout_json),
                    )
                    seats = t.get("seats") or []
                    for seat_idx, gid in enumerate(seats):
                        if gid is None:
                            continue
                        cur.execute(
                            "INSERT INTO seating_assignments "
                            "(day, table_key, seat_idx, guest_id) VALUES (?, ?, ?, ?)",
                            (d, table_key, seat_idx, int(gid)),
                        )

            # Per-guest meta
            for g in guests:
                gid = int(g["id"])
                tag_id = g.get("tagId")
                is_kid = 1 if g.get("isKid") else 0
                note = str(g.get("note") or "")
                # Skip rows that have no metadata to record (keeps the
                # table small and avoids resurrecting stale entries)
                if not tag_id and not is_kid and not note:
                    continue
                cur.execute(
                    "INSERT OR REPLACE INTO seating_guest_meta "
                    "(guest_id, tag_key, is_kid, note) VALUES (?, ?, ?, ?)",
                    (gid, tag_id, is_kid, note),
                )

            con.commit()
        except Exception:
            con.rollback()
            raise


# ── HTTP server ────────────────────────────────────────────────────────────

def _read_html() -> str:
    with open(HTML_PATH, "r", encoding="utf-8") as f:
        return f.read()


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIR, **kwargs)

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/", "/seating", "/seating/"):
            # Re-read on every request so edits to seating_app.html are
            # picked up without a server restart.
            self._send_html(_read_html())
            return
        if path == "/api/state":
            try:
                state = load_state()
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, status=500)
                return
            self._send_json(state)
            return
        # Anything else falls through to static file serving rooted at DIR
        super().do_GET()

    def do_POST(self):  # noqa: N802
        if self.path != "/api/state":
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
            save_state(payload)
        except ValueError as e:
            self._send_json({"ok": False, "error": str(e)}, status=400)
            return
        except Exception as e:  # pragma: no cover
            self._send_json({"ok": False, "error": str(e)}, status=500)
            return
        self._send_json({"ok": True})

    def log_message(self, format, *args):  # noqa: A002
        return

    def _send_html(self, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, obj: Any, status: int = 200) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


# ── Entry ──────────────────────────────────────────────────────────────────

def main() -> None:
    if not os.path.exists(DB_PATH):
        raise SystemExit(f"wedding.db not found at {DB_PATH}")
    if not os.path.exists(HTML_PATH):
        raise SystemExit(f"seating_app.html not found at {HTML_PATH}")

    ensure_schema()
    with _conn() as con:
        _migrate_initial(con)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}/seating"
    print(f"Seating chart editor running at {url}")
    print("Persists to wedding.db. Ctrl+C to stop.")

    threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
