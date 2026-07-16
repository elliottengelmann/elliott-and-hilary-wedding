#!/usr/bin/env python3
"""
One-off: import Guest Name pairings from master_list.csv into the
relationships table as romantic partners, tagged source='nima'.

Usage:
    python3 scripts/import_master_partners.py            # dry-run preview
    python3 scripts/import_master_partners.py --commit   # actually insert

Nima affirmed (CLAUDE.md two-source rule) that every Guest Name pairing
in master_list.csv is romantic, so this runs as a Nima-authored bulk
insert. This script is a one-shot — not part of merge_guests.py's
recurring pipeline.
"""
from __future__ import annotations

import csv
import os
import sqlite3
import sys

DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(DIR, "wedding.db")
MASTER_CSV = os.path.join(DIR, "master_list.csv")

LABEL = "partner"
SOURCE = "nima"

SKIP_PAIRS: set[frozenset[str]] = {
    frozenset(("turaj_gardideh", "dunja_gardideh")),
}


def normalize(name: str) -> str:
    return " ".join(name.strip().lower().split())


def guest_key(first: str, last: str) -> str:
    return f"{first.strip().lower()}_{last.strip().lower()}".replace(" ", "_")


def split_full_name(full: str) -> tuple[str, str]:
    full = (full or "").strip()
    if not full:
        return "", ""
    parts = full.rsplit(" ", 1)
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


def load_attending_index(conn: sqlite3.Connection) -> dict[tuple[str, str], str]:
    """Return {(norm_first, norm_last): guest_key} for attending guests."""
    idx = {}
    for row in conn.execute(
        "SELECT first_name, last_name FROM guests WHERE rsvp_status='Attending'"
    ):
        f, l = row[0] or "", row[1] or ""
        idx[(normalize(f), normalize(l))] = guest_key(f, l)
    return idx


def load_existing_pairs(conn: sqlite3.Connection) -> set[frozenset[str]]:
    pairs = set()
    for a, b in conn.execute("SELECT guest_a_key, guest_b_key FROM relationships"):
        pairs.add(frozenset((a, b)))
    return pairs


def main() -> None:
    commit = "--commit" in sys.argv

    if not os.path.exists(DB_PATH):
        sys.exit(f"wedding.db not found at {DB_PATH}")
    if not os.path.exists(MASTER_CSV):
        sys.exit(f"master_list.csv not found at {MASTER_CSV}")

    conn = sqlite3.connect(DB_PATH)
    try:
        attending = load_attending_index(conn)
        existing = load_existing_pairs(conn)

        to_insert: list[tuple[str, str, str, str]] = []
        skipped: list[str] = []
        unmatched: list[str] = []

        with open(MASTER_CSV, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                main_first = (row.get("FirstName") or "").strip()
                main_last = (row.get("Last Name") or "").strip()
                guest_name = (row.get("Guest Name") or "").strip()
                if not guest_name:
                    continue

                main_key_tuple = (normalize(main_first), normalize(main_last))
                main_k = attending.get(main_key_tuple)

                g_first, g_last = split_full_name(guest_name)
                guest_key_tuple = (normalize(g_first), normalize(g_last))
                guest_k = attending.get(guest_key_tuple)

                label = f"{main_first} {main_last} ↔ {guest_name}"

                if not main_k or not guest_k:
                    missing = []
                    if not main_k:
                        missing.append(f"main {main_first} {main_last}")
                    if not guest_k:
                        missing.append(f"guest {guest_name}")
                    unmatched.append(f"{label}  — no attending match for: {', '.join(missing)}")
                    continue

                if main_k == guest_k:
                    skipped.append(f"{label}  — self-pair")
                    continue

                pair = frozenset((main_k, guest_k))
                if pair in existing:
                    skipped.append(f"{label}  — already in relationships table")
                    continue
                if pair in SKIP_PAIRS:
                    skipped.append(f"{label}  — in SKIP_PAIRS (not romantic)")
                    continue

                to_insert.append((main_k, guest_k, LABEL, SOURCE))
                existing.add(pair)

        print(f"\nPairs to insert ({len(to_insert)}):")
        for a, b, lbl, src in to_insert:
            print(f"  + {a}  ↔  {b}   [{lbl}, source={src}]")

        if skipped:
            print(f"\nSkipped ({len(skipped)}):")
            for s in skipped:
                print(f"  - {s}")

        if unmatched:
            print(f"\nUnmatched ({len(unmatched)}) — not inserted:")
            for u in unmatched:
                print(f"  ? {u}")

        if not commit:
            print("\n(dry run — re-run with --commit to insert)")
            return

        if not to_insert:
            print("\nNothing to insert.")
            return

        conn.executemany(
            "INSERT INTO relationships (guest_a_key, guest_b_key, label, source) "
            "VALUES (?, ?, ?, ?)",
            to_insert,
        )
        conn.commit()
        print(f"\nInserted {len(to_insert)} rows into relationships.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
