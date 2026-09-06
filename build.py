#!/usr/bin/env python3
"""Build index.html from the SQLite database + HTML template."""

import hashlib
import html
import json
import os
import re
import shutil
import sqlite3
import subprocess
import urllib.parse
from datetime import datetime, timedelta

DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(DIR, "wedding.db")
OUT_PATH = os.path.join(DIR, "index.html")
SW_PATH = os.path.join(DIR, "sw.js")
MANIFEST_PATH = os.path.join(DIR, "manifest.webmanifest")
ICONS_DIR = os.path.join(DIR, "icons")

# Labels where "A is B's X" doesn't imply "B is A's X" (parent/child, etc.).
# For these, the label only applies on Person A's profile; Person B sees the
# name with no label suffix. Any other label is treated as symmetric.
DIRECTIONAL_LABELS = {
    "mom", "mother", "dad", "father",
    "son", "daughter",
    "stepmom", "stepdad", "stepson", "stepdaughter",
    "grandma", "grandpa", "grandmother", "grandfather",
    "grandson", "granddaughter",
    "aunt", "uncle", "niece", "nephew",
    "godmother", "godfather", "godchild",
    "mentor", "mentee",
}

# Labels that qualify a relationship as a romantic partner. Only these rows
# appear in the "Here with" section under the current-city facts — the
# intent is "who this guest came with," not the full social graph.
# Add new terms here as needed; matching is case-insensitive.
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

MEMORY_TITLES = {
    "them": "A memory of {name}",
    "nima": "A memory of Hilary",
    "elien": "A memory of Elliott",
    "both":  "A memory of Hilary and Elliott",
}

# Real guest photos live in images/guests/, resolved by merge_guests.py
# and stored as paths in wedding.db's `photo_url` column. Guests with
# no photo fall back to the colored-initials avatar (handled client-
# side in the template).
#
# Resized + face-cropped derivatives live in images/guests/derived/ and
# are produced by scripts/process_guest_images.py. The grid uses the
# 200x200 thumbnails; the profile uses the long-edge-1080 webp. If a
# derivative is missing for any reason, the JS falls back to the full-
# resolution source so missing-derivative is never a blocker.

DERIVED_DIR = os.path.join(DIR, "images", "guests", "derived")


def derived_variants_for(photo_url: str) -> dict:
    """Return relative paths for whichever derivatives exist on disk for
    a given source photo_url (e.g. 'images/guests/wiz_abdulla.jpg').
    Missing variants are simply absent from the returned dict so the
    template can degrade cleanly."""
    if not photo_url or not photo_url.startswith("images/guests/"):
        return {}
    rel = photo_url[len("images/guests/"):]
    stem, _ext = os.path.splitext(os.path.basename(rel))
    if not stem or "/" in rel:
        # Nested paths (e.g. images/guests/overrides/foo.png) intentionally
        # skip the derivative pipeline — they're hand-uploaded fallbacks.
        return {}
    out = {}
    for label, suffix in (
        ("thumbWebp", "-thumb.webp"),
        ("thumbJpg",  "-thumb.jpg"),
        ("fullWebp",  "-full.webp"),
    ):
        candidate = os.path.join(DERIVED_DIR, f"{stem}{suffix}")
        if os.path.exists(candidate):
            out[label] = f"images/guests/derived/{stem}{suffix}"
    return out


def query(conn, sql):
    c = conn.cursor()
    c.execute(sql)
    cols = [d[0] for d in c.description]
    return [dict(zip(cols, row)) for row in c.fetchall()]


def esc(text):
    """HTML-escape, returning empty string for None."""
    if text is None:
        return ""
    return html.escape(str(text))


# ── Fragment builders ───────────────────────────────────────────────────

WEDDING_YEAR = 2026
WEDDING_TZ = "America/New_York"


def _parse_clock(token: str):
    """Parse '6:00 PM' / '6 PM' / '10:30' (no meridiem). Returns (h, m, mer_or_None)."""
    token = token.strip()
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*([AP]M)?$", token, re.I)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2)) if m.group(2) else 0
    mer = m.group(3).upper() if m.group(3) else None
    return hour, minute, mer


def _to_24h(hour: int, mer: str):
    if mer == "PM" and hour != 12:
        return hour + 12
    if mer == "AM" and hour == 12:
        return 0
    return hour


def event_calendar_url(event):
    """Build a Google Calendar 'TEMPLATE' URL from an event row.

    Parses day_label (e.g. 'Thursday, 4/30') and time (e.g. '6:00 - 10:00 PM')
    rather than event_date because event_date is unreliable in the seed data.
    Returns None if parsing fails.
    """
    day_label = event["day_label"] if "day_label" in event.keys() else ""
    time_str = event["time"] if "time" in event.keys() else ""
    md = re.search(r"(\d{1,2})/(\d{1,2})", day_label or "")
    if not md:
        return None
    month, day = int(md.group(1)), int(md.group(2))
    base = datetime(WEDDING_YEAR, month, day)

    parts = re.split(r"\s*[\u2013\-]\s*", (time_str or "").strip())
    if len(parts) == 2:
        start_tok, end_tok = parts
        end = _parse_clock(end_tok)
        start = _parse_clock(start_tok)
        if not start or not end:
            return None
        # If start has no meridiem, inherit from end (e.g. "6:00 - 10:00 PM").
        if start[2] is None:
            start = (start[0], start[1], end[2])
        start_dt = base.replace(hour=_to_24h(start[0], start[2]), minute=start[1])
        end_dt = base.replace(hour=_to_24h(end[0], end[2]), minute=end[1])
        if end_dt <= start_dt:
            end_dt += timedelta(days=1)
    else:
        single = _parse_clock((time_str or "").strip())
        if not single or single[2] is None:
            return None
        start_dt = base.replace(hour=_to_24h(single[0], single[2]), minute=single[1])
        end_dt = start_dt + timedelta(hours=3)

    location = (event["location_label"] if "location_label" in event.keys() else "") \
        or (event["venue"] if "venue" in event.keys() else "")
    params = {
        "action": "TEMPLATE",
        "text": event["title"] if "title" in event.keys() else "",
        "dates": f"{start_dt.strftime('%Y%m%dT%H%M%S')}/{end_dt.strftime('%Y%m%dT%H%M%S')}",
        "ctz": WEDDING_TZ,
        "location": location or "",
    }
    note = event["note"] if "note" in event.keys() else ""
    if note:
        params["details"] = note
    return "https://calendar.google.com/calendar/render?" + urllib.parse.urlencode(params)


def event_map_url(event):
    """Prefer the curated location_url; otherwise build a Maps search from venue + address."""
    loc_url = (event["location_url"] if "location_url" in event.keys() else "") or ""
    loc_url = loc_url.strip()
    if loc_url:
        return loc_url
    venue = (event["venue"] if "venue" in event.keys() else "") or ""
    address = (event["location_label"] if "location_label" in event.keys() else "") or ""
    query_parts = [p.strip() for p in [venue, address] if p.strip()]
    if not query_parts:
        return None
    return "https://www.google.com/maps/search/?api=1&query=" + urllib.parse.quote(", ".join(query_parts))


def build_events(conn):
    events = query(conn, "SELECT * FROM events ORDER BY event_date")
    parts = []
    for e in events:
        # Build details rows
        details = ""

        if e["dress_code"]:
            details += f"""
                                <div class="detail-row">
                                    <div class="detail-label">Dress Code</div>
                                    <div class="detail-value">{esc(e["dress_code"])}</div>
                                </div>"""

        if e["note"]:
            details += f"""
                                <div class="detail-row">
                                    <div class="detail-label">From Us</div>
                                    <div class="detail-note">{esc(e["note"])}</div>
                                </div>"""

        if e["what_to_wear"] or (e["pinterest_women"] and e["pinterest_men"]):
            details += """
                                <div class="detail-row">
                                    <div class="detail-label">What To Wear</div>"""
            if e["what_to_wear"]:
                details += f"""
                                    <div class="detail-note">{esc(e["what_to_wear"])}</div>"""
            if e["pinterest_women"] and e["pinterest_men"]:
                details += f"""
                                    <div class="link-group">
                                        <a href="{esc(e["pinterest_women"])}" target="_blank">Women's Pinterest</a>
                                        <a href="{esc(e["pinterest_men"])}" target="_blank">Men's Pinterest</a>
                                    </div>"""
            details += """
                                </div>"""

        if e["location_label"]:
            details += f"""
                                <div class="detail-row">
                                    <div class="detail-label">Location</div>
                                    <div class="detail-value"><a href="{esc(e["location_url"])}" target="_blank">{esc(e["location_label"])}</a></div>
                                </div>"""

        action_links = []
        map_url = event_map_url(e)
        if map_url:
            action_links.append(
                f'<a href="{esc(map_url)}" target="_blank" rel="noopener" class="btn btn-secondary btn-small">Map</a>'
            )
        cal_url = event_calendar_url(e)
        if cal_url:
            action_links.append(
                f'<a href="{esc(cal_url)}" target="_blank" rel="noopener" class="btn btn-secondary btn-small">Add to Calendar</a>'
            )
        if action_links:
            details += """
                                <div class="action-buttons">
                                    """ + "\n                                    ".join(action_links) + """
                                </div>"""

        parts.append(f"""                    <div class="day-section">
                        <div class="event-card" onclick="toggleEvent(this, event)">
                            <div class="event-header">
                                <div class="day-label">{esc(e["day_label"])}</div>
                                <div class="event-title">{esc(e["title"])}</div>
                                <div class="event-time">{esc(e["time"])}</div>
                                <div class="venue-row">
                                    <div class="event-venue">{esc(e["venue"])}</div>
                                    <div class="expand-icon"><svg width="20" height="20" viewBox="0 0 40 40" fill="none"><path d="M10 12 C16 16, 17 26, 20 32 C23 26, 24 16, 30 12" stroke="#1F6E8C" stroke-width="3.5" fill="none" stroke-linecap="round"/></svg></div>
                                </div>
                            </div>
                            <div class="event-details">{details}
                            </div>
                        </div>
                    </div>""")

    return "\n\n".join(parts)


def build_extras(conn):
    """Extra activities — open-invite side events (yoga, etc.) that
    aren't part of the formal wedding-weekend schedule. Same card visual
    as Schedule (.event-card / .event-header / .event-details so the
    existing toggleEvent expand-on-tap and styling apply for free), but
    no RSVP wiring, no dress code, no attendance tracking — these events
    are additive and open to everyone who wants to come.

    Schema: id, event_date, day_label, title, time, venue,
    location_label, location_url, note, sort_order. day_label uses the
    same "Friday, 5/1" format as the events table so event_calendar_url
    can parse the date.
    """
    extras = query(
        conn,
        "SELECT * FROM extras ORDER BY event_date, sort_order, id",
    )
    parts = []
    for e in extras:
        details = ""

        if e["note"]:
            details += f"""
                                <div class="detail-row">
                                    <div class="detail-label">From Us</div>
                                    <div class="detail-note">{esc(e["note"])}</div>
                                </div>"""

        if e["location_label"] and e["location_url"]:
            details += f"""
                                <div class="detail-row">
                                    <div class="detail-label">Location</div>
                                    <div class="detail-value"><a href="{esc(e["location_url"])}" target="_blank">{esc(e["location_label"])}</a></div>
                                </div>"""

        action_links = []
        map_url = event_map_url(e)
        if map_url:
            action_links.append(
                f'<a href="{esc(map_url)}" target="_blank" rel="noopener" class="btn btn-secondary btn-small">Map</a>'
            )
        cal_url = event_calendar_url(e)
        if cal_url:
            action_links.append(
                f'<a href="{esc(cal_url)}" target="_blank" rel="noopener" class="btn btn-secondary btn-small">Add to Calendar</a>'
            )
        if action_links:
            details += """
                                <div class="action-buttons">
                                    """ + "\n                                    ".join(action_links) + """
                                </div>"""

        parts.append(f"""                    <div class="day-section">
                        <div class="event-card" onclick="toggleEvent(this, event)">
                            <div class="event-header">
                                <div class="day-label">{esc(e["day_label"])}</div>
                                <div class="event-title">{esc(e["title"])}</div>
                                <div class="event-time">{esc(e["time"])}</div>
                                <div class="venue-row">
                                    <div class="event-venue">{esc(e["venue"])}</div>
                                    <div class="expand-icon"><svg width="20" height="20" viewBox="0 0 40 40" fill="none"><path d="M10 12 C16 16, 17 26, 20 32 C23 26, 24 16, 30 12" stroke="#1F6E8C" stroke-width="3.5" fill="none" stroke-linecap="round"/></svg></div>
                                </div>
                            </div>
                            <div class="event-details">{details}
                            </div>
                        </div>
                    </div>""")

    return "\n\n".join(parts)


DAY_LABELS = [
    ("thursday", "Thursday"),
    ("friday", "Friday"),
    ("saturday", "Saturday"),
]


def build_toasts(conn):
    """
    Toasts are split by night (thursday vs friday vs saturday). Each card
    collapses by default; tapping the speaker header expands the toast
    body. The audio player (a real <audio> element wired up by
    initToastAudios in the page JS) only renders when an `audio_url` is
    present and is rendered FIRST (above the text) so listening is the
    primary affordance and the long Persian translation reads as
    accompanying material below. The CSS uses an `.audio-player +
    .toast-text` sibling rule for the divider between the two; when a
    toast has audio but no body text (e.g. someone whose Farsi
    translation isn't on file yet), we drop the empty toast-text div.
    Schema: id, speaker, text, duration, audio_url, day, sort_order.
    """
    toasts = query(
        conn,
        "SELECT * FROM toasts ORDER BY day, sort_order, id",
    )
    sections = []
    for day_key, day_label in DAY_LABELS:
        day_toasts = [t for t in toasts if (t["day"] or "") == day_key]
        if not day_toasts:
            continue
        cards = []
        for t in day_toasts:
            audio_url = (t["audio_url"] or "").strip() if "audio_url" in t.keys() else ""
            duration = (t["duration"] or "").strip()
            if audio_url:
                player_html = f"""
                            <div class="audio-player" onclick="event.stopPropagation()">
                                <button class="play-btn" type="button" aria-label="Play" onclick="toggleToastAudio(this, event)">▶</button>
                                <div class="progress-bar" onclick="seekToastAudio(this, event)"><div class="progress-fill"></div></div>
                                <div class="duration">{esc(duration)}</div>
                                <audio preload="none" src="{esc(audio_url)}"></audio>
                            </div>"""
            else:
                player_html = ""
            paragraphs = [p.strip() for p in (t["text"] or "").split("\n\n") if p.strip()]
            text_html = "\n                                ".join(
                f"<p>{esc(p).replace(chr(10), '<br>')}</p>" for p in paragraphs
            )
            text_block = (
                f"""<div class="toast-text">
                                {text_html}
                            </div>"""
                if paragraphs
                else ""
            )
            cards.append(
                f"""                    <div class="toast-card" onclick="toggleToast(this)">
                        <div class="toast-header">
                            <div class="toast-speaker">{esc(t["speaker"])}</div>
                            <div class="toast-toggle">▼</div>
                        </div>
                        <div class="toast-body">
                            {player_html}{text_block}
                        </div>
                    </div>"""
            )
        sections.append(
            f"""                    <h3 class="toast-day">{esc(day_label)}</h3>
{chr(10).join(cards)}"""
        )
    return "\n\n".join(sections)


# San Miguel Guide section ordering. "Coffee & Espresso" is sourced from
# the cafes table; everything else from guide_places.category. Sections
# with zero visible items don't render at all (and are dropped from the
# jump nav too). User-facing labels — keep verbatim.
GUIDE_SECTION_ORDER = [
    "Restaurants",
    "Bars",
    "Coffee & Espresso",
    "Activities",
    "Historical Sites",
    "Shopping",
    "Grocery",
]

# Per-section Google-Maps shortcuts. Each section optionally gets one or
# more pinned links at the top before its cards — Elien's curated maps
# for that category. Banned outside this dict; never auto-generated.
GUIDE_SECTION_MAPS = {
    # Optional pinned Google-Maps links per guide section — Hilary & Elliott's
    # own curated maps go here. Empty until they add theirs.
}

GUIDE_SECTION_SLUG = {
    "Restaurants": "guide-restaurants",
    "Bars": "guide-bars",
    "Coffee & Espresso": "guide-coffee",
    "Activities": "guide-activities",
    "Historical Sites": "guide-historical",
    "Shopping": "guide-shopping",
    "Grocery": "guide-grocery",
}

# Small inline sun icon flagging rooftop places. Hand-drawn line-art per
# the design system; stroke inherits from .place-rooftop.
ROOFTOP_ICON_SVG = (
    '<span class="place-rooftop" title="rooftop" aria-label="rooftop">'
    '<svg viewBox="0 0 16 16" fill="none" aria-hidden="true">'
    '<circle cx="8" cy="9" r="2.3" stroke-width="1.3" fill="none"/>'
    '<line x1="8" y1="3" x2="8" y2="4.5" stroke-width="1.3" stroke-linecap="round"/>'
    '<line x1="3.5" y1="9" x2="5" y2="9" stroke-width="1.3" stroke-linecap="round"/>'
    '<line x1="11" y1="9" x2="12.5" y2="9" stroke-width="1.3" stroke-linecap="round"/>'
    '<line x1="4.5" y1="5.5" x2="5.5" y2="6.5" stroke-width="1.3" stroke-linecap="round"/>'
    '<line x1="10.5" y1="6.5" x2="11.5" y2="5.5" stroke-width="1.3" stroke-linecap="round"/>'
    '</svg></span>'
)


def _place_card_html(p, has_rooftop_flag=False):
    """Travel-style collapsible card. The HEADER previews the place: name
    + Elien's description (line-clamped to 2 lines via CSS when collapsed,
    full when expanded). The BODY holds address + Get Directions, revealed
    by tapping the disclosure arrow.

    A card is only collapsible if it has a body (address or directions
    or a photo); otherwise it renders as a static card with no arrow."""
    note = (p["note"] or "").strip() if "note" in p.keys() else ""
    address = (p["address"] or "").strip() if "address" in p.keys() else ""
    directions = (p["directions_url"] or "").strip() if "directions_url" in p.keys() else ""
    category = (p["category"] or "").strip() if "category" in p.keys() else ""
    photo_path = (p["photo_path"] or "").strip() if "photo_path" in p.keys() else ""
    name_html = f'{esc(p["name"])}{ROOFTOP_ICON_SVG if has_rooftop_flag else ""}'

    # Photos currently scoped to Shopping per the CMS — render only there
    # so a stale photo on a category-changed row doesn't surface on the
    # live site by surprise. Loaded lazily; the user pays nothing for
    # the bytes until they tap to expand the card.
    photo_html = ""
    if photo_path and category == "Shopping":
        photo_html = (
            f'<div class="place-photo">'
            f'<picture>'
            f'<source srcset="{esc(photo_path)}.webp" type="image/webp">'
            f'<img src="{esc(photo_path)}.jpg" alt="" loading="lazy" decoding="async">'
            f'</picture>'
            f'</div>'
        )

    note_html = (
        f'<div class="place-note">{esc(note)}</div>' if note else ""
    )
    address_html = (
        f'<div class="place-address">{esc(address)}</div>' if address else ""
    )
    directions_html = (
        f'<a href="{esc(directions)}" target="_blank" rel="noopener" '
        f'class="place-directions" onclick="event.stopPropagation()">Get Directions \u2192</a>'
        if directions else ""
    )
    body_inner = (photo_html + address_html + directions_html).strip()
    if not body_inner:
        # Nothing to expand — render static, no arrow. Description still
        # shows in full (the line-clamp only applies to .collapsible cards).
        return (
            '                            <div class="place-card">\n'
            '                                <div class="place-card-header place-card-header--static">\n'
            '                                    <div class="place-card-summary">\n'
            f'                                        <div class="place-name">{name_html}</div>\n'
            f'                                        {note_html}\n'
            '                                    </div>\n'
            '                                </div>\n'
            '                            </div>'
        )
    return (
        '                            <div class="place-card collapsible">\n'
        '                                <div class="place-card-header" onclick="togglePlaceCard(this.parentElement)">\n'
        '                                    <div class="place-card-summary">\n'
        f'                                        <div class="place-name">{name_html}</div>\n'
        f'                                        {note_html}\n'
        '                                    </div>\n'
        '                                    <div class="place-card-arrow" aria-hidden="true">\u25be</div>\n'
        '                                </div>\n'
        '                                <div class="place-card-body">\n'
        f'                                    {body_inner}\n'
        '                                </div>\n'
        '                            </div>'
    )


def _section_maps_html(label):
    """Pinned Google-Maps links at the top of a section. Empty string if
    the section has no curated maps."""
    links = GUIDE_SECTION_MAPS.get(label, [])
    if not links:
        return ""
    parts = []
    for link_label, url in links:
        parts.append(
            f'                            <a href="{esc(url)}" target="_blank" rel="noopener" '
            f'class="guide-map-link">'
            f'<span class="guide-map-icon" aria-hidden="true">'
            f'<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round">'
            f'<path d="M8 1.5 C5 1.5 3 3.5 3 6.5 C3 10 8 14.5 8 14.5 C8 14.5 13 10 13 6.5 C13 3.5 11 1.5 8 1.5 Z"/>'
            f'<circle cx="8" cy="6.5" r="1.6" fill="none"/>'
            f'</svg>'
            f'</span>{esc(link_label)} \u2192</a>'
        )
    return (
        '                        <div class="guide-section-maps">\n'
        + "\n".join(parts)
        + "\n                        </div>"
    )


def build_guide(conn):
    """
    San Miguel Guide. Renders sections in GUIDE_SECTION_ORDER, only
    including sections with at least one visible item. Each section
    starts with optional pinned Google-Maps links, then a stack of
    collapsible Travel-style place-cards. A jump nav at the top of
    the page lets users skip directly to any visible section.

    "Coffee & Espresso" is sourced from the cafes table. Everything
    else from guide_places, filtered by the admin's `visible` flag.
    """
    places = query(
        conn,
        "SELECT * FROM guide_places WHERE COALESCE(visible, 1) = 1 ORDER BY id",
    )
    cafes = query(
        conn,
        "SELECT * FROM cafes WHERE COALESCE(visible, 1) = 1 ORDER BY id",
    )

    # Build (label, items, has_rooftop_per_item) for each section.
    items_by_section = []
    for label in GUIDE_SECTION_ORDER:
        if label == "Coffee & Espresso":
            rows = cafes
            rooftops = [False] * len(rows)
        else:
            rows = [p for p in places if (p["category"] or "") == label]
            rooftops = [
                bool(p["has_rooftop"] if "has_rooftop" in p.keys() else 0) for p in rows
            ]
        if rows:
            items_by_section.append((label, rows, rooftops))

    if not items_by_section:
        return ""

    # Jump-nav pill list — only the sections we're actually about to render.
    nav_pills = "\n".join(
        f'                            <button type="button" class="guide-jump-pill" '
        f'onclick="scrollToGuideSection(\'{GUIDE_SECTION_SLUG[label]}\')">{esc(label)}</button>'
        for (label, _, _) in items_by_section
    )

    sections_html = []
    for (label, rows, rooftops) in items_by_section:
        cards = "\n".join(_place_card_html(p, rooftops[i]) for i, p in enumerate(rows))
        maps_html = _section_maps_html(label)
        section_id = GUIDE_SECTION_SLUG[label]
        sections_html.append(
            f"""                    <section class="guide-section" id="{section_id}">
                        <h3 class="guide-section-title">
                            <button type="button" class="guide-section-toggle" onclick="toggleGuideSection(this.closest('.guide-section'))" aria-expanded="true" aria-controls="{section_id}-body">
                                <span>{esc(label)}</span>
                                <span class="guide-section-arrow" aria-hidden="true"><svg width="20" height="20" viewBox="0 0 40 40" fill="none"><path d="M10 12 C16 16, 17 26, 20 32 C23 26, 24 16, 30 12" stroke="#1F6E8C" stroke-width="3.5" fill="none" stroke-linecap="round"/></svg></span>
                            </button>
                        </h3>
                        <div class="guide-section-body" id="{section_id}-body">
{maps_html if maps_html else ''}
                            <div class="guide-section-cards">
{cards}
                            </div>
                        </div>
                    </section>"""
        )

    return (
        '                    <nav class="guide-jump-nav" aria-label="Jump to section">\n'
        + nav_pills
        + "\n                    </nav>\n\n"
        + "\n\n".join(sections_html)
    )


def build_faqs(conn):
    faqs = query(conn, "SELECT * FROM faqs ORDER BY id")
    parts = []
    for f in faqs:
        parts.append(f"""                    <div class="accordion-item" onclick="toggleAccordion(this)">
                        <div class="accordion-header">
                            <div class="accordion-question">{esc(f["question"])}</div>
                            <div class="accordion-toggle">▼</div>
                        </div>
                        <div class="accordion-content">{esc(f["answer"])}</div>
                    </div>""")
    return "\n\n".join(parts)


def build_airports(conn):
    airports = query(conn, "SELECT * FROM airports ORDER BY id")
    parts = []
    for a in airports:
        parts.append(f"""                        <div style="background: white; border: 1px solid var(--divider); border-radius: 12px; padding: 16px; margin-bottom: 12px;">
                            <div style="font-family: 'Bodoni Moda', serif; font-size: 15px; font-weight: 600; color: var(--dark); margin-bottom: 4px;">{esc(a["name"])} ({esc(a["code"])})</div>
                            <div style="font-family: 'Nunito', sans-serif; font-size: 14px; color: var(--dark); line-height: 1.6;">{esc(a["description"])}</div>
                        </div>""")
    return "\n\n".join(parts)


def build_car_services(conn):
    services = query(conn, "SELECT * FROM car_services ORDER BY id")
    parts = []
    for s in services:
        inner = ""
        inner += f"""<div style="font-family: 'Bodoni Moda', serif; font-size: 15px; font-weight: 600; color: var(--dark); margin-bottom: 6px;">{esc(s["name"])}</div>"""
        if s["description"]:
            inner += f"""
                            <div style="font-family: 'Nunito', sans-serif; font-size: 14px; color: var(--dark); line-height: 1.5; margin-bottom: 8px;">{esc(s["description"])}</div>"""
        if s["contact"]:
            inner += f"""
                            <a href="{esc(s["url"])}" target="_blank" style="font-family: 'Nunito', sans-serif; font-size: 14px; color: var(--primary-green); display: block; margin-bottom: 8px;">{esc(s["contact"])}</a>"""
        if s["pricing"]:
            lines = s["pricing"].split(" | ")
            pricing_html = "<br>\n                                ".join(esc(l) for l in lines)
            inner += f"""
                            <div style="font-family: 'Nunito', sans-serif; font-size: 14px; color: var(--dark); line-height: 1.7;">
                                {pricing_html}
                            </div>"""
        parts.append(f"""                        <div style="background: white; border: 1px solid var(--divider); border-radius: 12px; padding: 16px; margin-bottom: 12px;">
                            {inner}
                        </div>""")
    return "\n\n".join(parts)


def guest_key(first, last):
    """Stable key for joining overrides.json to a guest record."""
    return f"{(first or '').strip().lower()}_{(last or '').strip().lower()}".replace(" ", "_")


RELATIONSHIP_SOURCES = {"elien", "nima"}


def load_curation(conn):
    """
    Load Elien/Nima-authored curation from wedding.db — the only
    source of truth. Returns a dict shaped like the old overrides
    JSON so downstream builders don't have to care.
    """
    locations = {}
    for key, city, hometown in conn.execute(
        "SELECT guest_key, current_city, hometown FROM guest_locations"
    ):
        locations[key] = {"currentCity": city or "", "hometown": hometown or ""}

    memories = []
    for guest_key, subject, text, source in conn.execute(
        "SELECT guest_key, subject, text, source FROM guest_memories"
    ):
        memories.append({"guest": guest_key, "subject": subject, "text": text, "source": source})

    relationships = []
    for a, b, label, source in conn.execute(
        "SELECT guest_a_key, guest_b_key, label, source FROM relationships"
    ):
        relationships.append({"a": a, "b": b, "label": label or "", "source": source})

    # Per-field admin overrides of form-sourced values (how_we_know,
    # least_favorite, photo_url). Absent row = use form value.
    field_overrides: dict[str, dict[str, str]] = {}
    try:
        rows = conn.execute(
            "SELECT guest_key, field, value FROM guest_field_overrides"
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []  # table may not yet exist on first run before editor touches the db
    for guest_key, field, value in rows:
        field_overrides.setdefault(guest_key, {})[field] = value

    # Post-wedding contact info — phone, email, socials. Populated from
    # contact_form_responses.csv by merge_guests.merge_contacts() (rows
    # tagged source='form') and from manual edits in edit_guests.py
    # (source='elien'/'nima'). Either source renders identically on the
    # profile; the source tag only governs whose write wins when both
    # paths touch the same guest. See CLAUDE.md → "guest_contacts".
    contacts: dict[str, dict[str, str]] = {}
    try:
        rows = conn.execute(
            "SELECT guest_key, phone, email, instagram, linkedin, "
            "twitter, bluesky, soundcloud, source FROM guest_contacts"
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []  # table may not yet exist on a fresh checkout
    for guest_key, phone, email, ig, li, tw, bs, sc, source in rows:
        if source not in ("form", "elien", "nima"):
            # Schema CHECK constraint should prevent this, but defence
            # in depth — never render an un-sourced contact row.
            raise SystemExit(
                f"BUILD ABORTED — guest_contacts row for {guest_key!r} has "
                f"invalid source {source!r}. Allowed: form, elien, nima."
            )
        contacts[guest_key] = {
            "phone":      phone or "",
            "email":      email or "",
            "instagram":  ig or "",
            "linkedin":   li or "",
            "twitter":    tw or "",
            "bluesky":    bs or "",
            "soundcloud": sc or "",
        }

    _validate_relationship_sources(relationships)
    return {
        "locations": locations,
        "memories": memories,
        "relationships": relationships,
        "fieldOverrides": field_overrides,
        "contacts": contacts,
    }


def _validate_relationship_sources(rows):
    """
    Enforce the "Here with" source rule (see CLAUDE.md): every
    relationship row must carry a source of 'elien' or 'nima'. Abort
    the build on the first offender — do not render un-sourced or
    AI/inferred relationships. The schema-level CHECK constraint in
    wedding.db provides a second layer of defence.
    """
    bad = [(i, r) for i, r in enumerate(rows) if r.get("source") not in RELATIONSHIP_SOURCES]
    if not bad:
        return
    lines = [
        "",
        "  BUILD ABORTED — relationship rows with invalid `source`.",
        f"  Every row in wedding.db `relationships` must have source ∈ {sorted(RELATIONSHIP_SOURCES)!r}.",
        "  Offending rows:",
    ]
    for i, r in bad:
        lines.append(f"    [{i}] a={r.get('a')!r}  b={r.get('b')!r}  label={r.get('label')!r}  source={r.get('source')!r}")
    lines.append("")
    lines.append("  See CLAUDE.md → \"The Here with source rule\".")
    raise SystemExit("\n".join(lines))


def build_memories_for(key, overrides, display_name):
    """Return memory objects {title, text} for a guest, in a stable order."""
    order = ["them", "nima", "elien", "both"]
    out = []
    for subject in order:
        for m in overrides["memories"]:
            if m.get("guest") == key and m.get("subject") == subject and (m.get("text") or "").strip():
                title = MEMORY_TITLES[subject].format(name=display_name)
                out.append({"title": title, "text": m["text"].strip()})
    return out


def build_herewith_for(key, overrides, name_by_key):
    """
    Return [{key, name}] for a guest's "Here with" line — romantic
    partners only (spouse, date, fiancé, etc.). All other relationship
    types (family, friends) are intentionally excluded; they'll surface
    in other sections to be added later.
    """
    partners = {}
    for r in overrides["relationships"]:
        a, b = r.get("a"), r.get("b")
        label = (r.get("label") or "").strip().lower()
        if not a or not b or a == b:
            continue
        if label not in ROMANTIC_LABELS:
            continue
        if key == a and b in name_by_key:
            partners[b] = {"key": b, "name": name_by_key[b]}
        elif key == b and a in name_by_key:
            partners[a] = {"key": a, "name": name_by_key[a]}
    return sorted(partners.values(), key=lambda c: c["name"].lower())


def build_guest_json(conn):
    # Anyone who filled out the form gets a profile in Who's Coming,
    # regardless of their RSVP status. Whether they've formally ticked
    # "Attending" on the RSVP is not the gate — a submitted form
    # answer is. (An admin story override also unlocks a profile, for
    # cases where Elien or Nima add the story themselves.)
    #
    # Returns a (guests_json, partner_profiles_json) pair. partner_profiles
    # holds minimal "linked-partner" stubs: attending guests who haven't
    # filled out the form but are referenced as the romantic partner of a
    # story-having guest. They never appear in the browse grid; they only
    # exist as link targets when a partner chip points at them.
    all_guests = query(conn, "SELECT * FROM guests ORDER BY last_name, first_name")
    overrides = load_curation(conn)
    field_overrides = overrides.get("fieldOverrides", {})
    contacts_by_key = overrides.get("contacts", {})

    def story_for(g):
        key = guest_key((g.get("first_name") or ""), (g.get("last_name") or ""))
        fo = field_overrides.get(key, {})
        return (fo.get("how_we_know") or g.get("how_we_know") or "").strip()

    def has_contacts_for(g):
        """A guest with a non-empty `guest_contacts` row qualifies for the
        main grid even without a 'how we know' story — sharing post-wedding
        contact info is also a form of engagement, and historically these
        guests showed only as linked-partner stubs. Their partner edges
        still come from the `relationships` table, so chips between them
        and a story-having guest resolve normally."""
        key = guest_key((g.get("first_name") or ""), (g.get("last_name") or ""))
        row = contacts_by_key.get(key) or {}
        return any(v for k, v in row.items() if k != "source")

    guests = [g for g in all_guests if story_for(g) or has_contacts_for(g)]

    # Index every guest by key (story-having or not) so we can look up
    # display name + photo for linked-partner stubs without re-querying.
    guest_by_key = {}
    for g in all_guests:
        k = guest_key((g.get("first_name") or ""), (g.get("last_name") or ""))
        guest_by_key[k] = g

    visible_keys = {
        guest_key((g.get("first_name") or ""), (g.get("last_name") or ""))
        for g in guests
    }

    # Linked-partner keys: attending guests with no story who are
    # referenced as the *romantic* partner of a visible guest. The
    # editor allows entering a "Here with" pointing at someone who
    # hasn't filled out the form yet; this is what makes those chips
    # render and link out to a minimal profile page.
    linked_partner_keys = set()
    for r in overrides["relationships"]:
        a, b = r.get("a"), r.get("b")
        label = (r.get("label") or "").strip().lower()
        if not a or not b or label not in ROMANTIC_LABELS:
            continue
        for partner_key, anchor_key in ((a, b), (b, a)):
            if partner_key in visible_keys or anchor_key not in visible_keys:
                continue
            partner_row = guest_by_key.get(partner_key)
            if not partner_row:
                continue
            if (partner_row.get("rsvp_status") or "").strip() == "Attending":
                linked_partner_keys.add(partner_key)

    # First pass: build key → display-name map so relationship chips
    # can label each other. Includes both the visible (story-having)
    # guests and linked-partner stubs so chips pointing at a non-story
    # guest still render with their name. Honors the display_name
    # override (e.g. "Elien Blue Becque") so chips read the same name
    # as the profile + grid.
    name_by_key = {}
    for g in guests:
        first = (g.get("first_name") or "").strip()
        last = (g.get("last_name") or "").strip()
        k = guest_key(first, last)
        fo = field_overrides.get(k, {})
        override_name = (fo.get("display_name") or "").strip()
        name_by_key[k] = override_name or f"{first} {last}".strip()
    for k in linked_partner_keys:
        g = guest_by_key[k]
        first = (g.get("first_name") or "").strip()
        last = (g.get("last_name") or "").strip()
        fo = field_overrides.get(k, {})
        override_name = (fo.get("display_name") or "").strip()
        name_by_key[k] = override_name or f"{first} {last}".strip()

    js_guests = []
    for g in guests:
        first = (g.get("first_name") or "").strip()
        last = (g.get("last_name") or "").strip()
        key = guest_key(first, last)
        fo = field_overrides.get(key, {})
        # display_name override wins for the rendered name everywhere on the
        # site. firstName / lastName stay tied to the CSV import so login
        # fuzzy-match and alphabetical sort keep working.
        override_name = (fo.get("display_name") or "").strip()
        display = override_name or f"{first} {last}".strip()

        # Admin override wins over form value on all override-able fields.
        photo = (fo.get("photo_url") or g.get("photo_url") or "").strip()
        story = (fo.get("how_we_know") or g.get("how_we_know") or "").strip()
        least_fav = (fo.get("least_favorite") or g.get("least_favorite") or "").strip()
        pronouns = (g.get("pronouns") or "").strip()
        thu = (fo.get("rsvp_thursday") or g.get("rsvp_thursday") or "").strip()
        fri = (fo.get("rsvp_friday") or g.get("rsvp_friday") or "").strip()
        sat = (fo.get("rsvp_wedding") or g.get("rsvp_wedding") or "").strip()
        sun = (fo.get("rsvp_sunday") or g.get("rsvp_sunday") or "").strip()
        # Events copy: list of every event the guest has RSVPed Yes to,
        # in chronological order. Rendered one-per-line on the profile,
        # so this is sent over as an array of strings. Only "Attending"
        # counts as a yes; anything else reads as no. Phrasing is fixed
        # copy ("Saturday Wedding Ceremony", "Sunday Come Down Dinner")
        # so the Who's Coming search filter can still match by
        # day-of-week (substring match against the array's toString()).
        attending_events = []
        if thu.lower() == "attending":
            attending_events.append("Thursday Welcome Dinner")
        if fri.lower() == "attending":
            attending_events.append("Friday Welcome Dinner")
        if sat.lower() == "attending":
            attending_events.append("Saturday Wedding Ceremony")
        if sun.lower() == "attending":
            attending_events.append("Sunday Come Down Dinner")

        loc = overrides["locations"].get(key, {})
        # city/hometown: Elien/Nima curation wins; otherwise fall back
        # to whatever the guest wrote on the Google Form.
        city = (loc.get("currentCity") or "").strip() or (g.get("form_current_city") or "").strip()
        hometown = (loc.get("hometown") or "").strip() or (g.get("form_hometown") or "").strip()

        # Post-wedding contact info, if the guest shared any. Stripped of
        # empty fields so the front end can simply check `contacts` for
        # truthiness or iterate Object.keys(contacts).length without
        # rendering blank rows for unset platforms.
        contact_row = overrides.get("contacts", {}).get(key) or {}
        contacts = {k: v for k, v in contact_row.items() if v}

        obj = {
            "key": key,
            "firstName": first,
            "lastName": last,
            "name": display,
            "city": city,
            "hometown": hometown,
            "pronouns": pronouns,
            "story": story,
            "leastFavorite": least_fav,
            "guestMemory": (g.get("form_memory") or "").strip(),
            "initials": ((first[:1] + last[:1]) or "").upper(),
            "photoUrl": photo,
            "memories": build_memories_for(key, overrides, display),
            "hereWith": build_herewith_for(key, overrides, name_by_key),
            "events": attending_events,
            "contacts": contacts,
            "hasContacts": bool(contacts),
        }
        obj.update(derived_variants_for(photo))
        js_guests.append(obj)

    # Linked-partner stubs — minimal profile data for an attending guest
    # who hasn't filled the form but is the romantic partner of a visible
    # guest. They never enter the browse grid or swipe sequence; the
    # front end only opens them when a "Here with" chip targets their
    # key. Photo override + initials are surfaced so the empty profile
    # at least shows a face if Elien curated one.
    js_partner_profiles = []
    for k in sorted(linked_partner_keys):
        g = guest_by_key[k]
        first = (g.get("first_name") or "").strip()
        last = (g.get("last_name") or "").strip()
        fo = field_overrides.get(k, {})
        override_name = (fo.get("display_name") or "").strip()
        display = override_name or f"{first} {last}".strip()
        photo = (fo.get("photo_url") or g.get("photo_url") or "").strip()
        stub = {
            "key": k,
            "firstName": first,
            "lastName": last,
            "name": display,
            "initials": ((first[:1] + last[:1]) or "").upper(),
            "photoUrl": photo,
        }
        stub.update(derived_variants_for(photo))
        js_partner_profiles.append(stub)

    return (
        json.dumps(js_guests, ensure_ascii=False, indent=12),
        json.dumps(js_partner_profiles, ensure_ascii=False, indent=12),
    )


def build_hero_preloads_for(guest_json: str, count: int = 6) -> str:
    """Emit <link rel="preload"> tags for the first `count` guests in
    display order that have a thumbnail derivative. These preloads kick
    off as the document parses — meaningfully earlier than the JS-driven
    grid render, which has to wait for the inline script to run."""
    try:
        guests = json.loads(guest_json)
    except json.JSONDecodeError:
        return ""
    links = []
    for g in guests:
        url = g.get("thumbWebp")
        if not url:
            continue
        links.append(
            f'    <link rel="preload" as="image" type="image/webp" href="{esc(url)}">'
        )
        if len(links) >= count:
            break
    return "\n".join(links)


def build_guest_lookup_json(conn):
    """Compact guest lookup for fuzzy login matching and RSVP-based schedule filtering."""
    guests = query(conn, "SELECT * FROM guests ORDER BY id")
    out = []
    for g in guests:
        out.append({
            "i": g["id"],
            "f": (g.get("first_name") or "").strip(),
            "l": (g.get("last_name") or "").strip(),
            "n": (g.get("full_name") or "").strip(),
            "t": g.get("rsvp_thursday") or "",
            "r": g.get("rsvp_friday") or "",
            "w": g.get("rsvp_wedding") or "",
            "s": g.get("rsvp_sunday") or "",
        })
    return json.dumps(out, ensure_ascii=False, separators=(",", ":"))


# ── Minification ────────────────────────────────────────────────────────

def minify(html_str: str) -> str:
    """Run the rendered HTML through html-minifier-terser (HTML + inline
    CSS via clean-css + inline JS via terser). Falls back to the original
    string with a warning if npx/node isn't available so the build still
    works in environments without Node."""
    if shutil.which("npx") is None:
        print("\u26a0 npx not found - skipping minification.")
        return html_str

    args = [
        "npx", "--yes", "html-minifier-terser",
        "--collapse-whitespace",
        "--remove-comments",
        "--collapse-boolean-attributes",
        "--remove-redundant-attributes",
        "--decode-entities",
        "--minify-css", "true",
        "--minify-js", "true",
    ]
    try:
        result = subprocess.run(
            args, input=html_str, capture_output=True, text=True, check=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"\u26a0 html-minifier-terser failed ({e.returncode}); writing unminified output.")
        if e.stderr:
            print(e.stderr.strip())
        return html_str
    return result.stdout


# ── Main ────────────────────────────────────────────────────────────────

def main():
    conn = sqlite3.connect(DB_PATH)

    events_html = build_events(conn)
    extras_html = build_extras(conn)
    toasts_html = build_toasts(conn)
    guide_html = build_guide(conn)
    faqs_html = build_faqs(conn)
    airports_html = build_airports(conn)
    car_services_html = build_car_services(conn)
    guest_json, partner_profiles_json = build_guest_json(conn)
    guest_lookup_json = build_guest_lookup_json(conn)

    conn.close()

    output = TEMPLATE.replace("{{EVENTS}}", events_html)
    output = output.replace("{{EXTRAS}}", extras_html)
    output = output.replace("{{TOASTS}}", toasts_html)
    output = output.replace("{{GUIDE}}", guide_html)
    output = output.replace("{{FAQS}}", faqs_html)
    output = output.replace("{{AIRPORTS}}", airports_html)
    output = output.replace("{{CAR_SERVICES}}", car_services_html)
    output = output.replace("{{GUESTS_JSON}}", guest_json)
    output = output.replace("{{PARTNER_PROFILES_JSON}}", partner_profiles_json)
    output = output.replace("{{GUEST_LOOKUP_JSON}}", guest_lookup_json)
    output = output.replace("{{HERO_PRELOADS}}", build_hero_preloads_for(guest_json))

    raw_size = len(output.encode("utf-8"))
    output = minify(output)
    # Compute the version hash on the minified output *before* substituting
    # {{VERSION}} — replacing the placeholder with the hash itself would
    # change the bytes the hash is computed over. The same hash is written
    # into both index.html (visible at the bottom of the hamburger menu)
    # and sw.js's CACHE_VERSION, so the on-page version always matches what
    # the installed PWA is actually serving. Icon bytes are folded in too,
    # so swapping just the PNGs (with no HTML/data change) still bumps the
    # cache key — otherwise installed PWAs cling to their cached
    # `?v=OLD` icon URLs and never see the new artwork.
    hasher = hashlib.sha256()
    hasher.update(output.encode("utf-8"))
    for name in sorted(os.listdir(ICONS_DIR)):
        path = os.path.join(ICONS_DIR, name)
        if not os.path.isfile(path) or name.startswith("."):
            continue
        hasher.update(b"\0")
        hasher.update(name.encode("utf-8"))
        hasher.update(b"\0")
        with open(path, "rb") as f:
            hasher.update(f.read())
    version = hasher.hexdigest()[:12]
    output = output.replace("{{VERSION}}", version)
    minified_size = len(output.encode("utf-8"))

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write(output)

    write_sw_cache_version(version)
    write_manifest_icon_versions(version)

    if minified_size < raw_size:
        saved = raw_size - minified_size
        pct = saved / raw_size * 100
        print(
            f"Built {OUT_PATH} "
            f"({raw_size:,} \u2192 {minified_size:,} bytes, -{saved:,} / {pct:.1f}%)"
        )
    else:
        print(f"Built {OUT_PATH} ({minified_size:,} bytes, unminified)")


def write_sw_cache_version(version: str) -> None:
    """Write the given version hash into sw.js's CACHE_VERSION line. The SW
    only triggers an update on installed PWAs when its own bytes change, so
    without this any content change that didn't manually touch sw.js would
    silently fail to propagate. The hash is computed once in main() over
    the minified output (with {{VERSION}} placeholder still in it) so the
    in-page version indicator and the SW cache key agree."""
    with open(SW_PATH, "r", encoding="utf-8") as f:
        sw_src = f.read()
    new_sw_src, n = re.subn(
        r"const CACHE_VERSION = '[^']*';",
        f"const CACHE_VERSION = '{version}';",
        sw_src,
        count=1,
    )
    if n != 1:
        raise SystemExit(
            "build.py: could not find `const CACHE_VERSION = '...';` in sw.js"
        )
    if new_sw_src != sw_src:
        with open(SW_PATH, "w", encoding="utf-8") as f:
            f.write(new_sw_src)
        print(f"Bumped CACHE_VERSION in sw.js \u2192 '{version}'")


def write_manifest_icon_versions(version: str) -> None:
    """Append ?v=<version> to each /icons/* src in manifest.webmanifest so
    a deploy invalidates the URL — iOS keys the home-screen icon by URL,
    so without this the PNG bytes on disk can change but the OS keeps the
    icon it grabbed at install time. Same hash as sw.js's CACHE_VERSION
    and the icon refs in index.html's <head>; all three move together
    so the OS is forced to re-fetch."""
    with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
        src = f.read()
    new_src = re.sub(
        r'"src":\s*"(/icons/[^"?]+)(?:\?v=[^"]*)?"',
        lambda m: f'"src": "{m.group(1)}?v={version}"',
        src,
    )
    if new_src != src:
        with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
            f.write(new_src)
        print(f"Bumped icon ?v= in manifest.webmanifest \u2192 '{version}'")


# ── Template ────────────────────────────────────────────────────────────

TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
    <title>Hilary & Elliott's Wedding</title>
    <meta name="description" content="Hilary & Elliott's wedding.">

    <!-- ?v={{VERSION}} on every icon URL is the only reliable cache-bust
         for installed PWAs. iOS keeps the apple-touch-icon it grabbed at
         "Add to Home Screen" time keyed by exact URL — so unless the URL
         itself changes, swapping the PNG bytes on disk doesn't propagate
         even on reinstall. {{VERSION}} is the same content hash baked
         into sw.js's CACHE_VERSION, so a deploy bumps both in lockstep. -->
    <link rel="manifest" href="manifest.webmanifest?v={{VERSION}}">
    <meta name="theme-color" content="#1F6E8C">
    <meta name="color-scheme" content="light">

    <link rel="icon" type="image/svg+xml" href="icons/icon.svg?v={{VERSION}}">
    <link rel="icon" type="image/png" sizes="192x192" href="icons/icon-192.png?v={{VERSION}}">
    <link rel="icon" type="image/png" sizes="512x512" href="icons/icon-512.png?v={{VERSION}}">
    <link rel="apple-touch-icon" href="icons/apple-touch-icon.png?v={{VERSION}}">

    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="default">
    <meta name="apple-mobile-web-app-title" content="Hilary & Elliott">

    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Mea+Culpa&family=Bodoni+Moda:ital,wght@0,400..900;1,400..900&family=Nunito:wght@400;500;600;700&family=Lateef:wght@400;700&display=swap" rel="stylesheet">

{{HERO_PRELOADS}}

    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        :root {
            --primary-green: #1F6E8C;
            --light-bg: #F5F0EB;
            --light-sage: #CDE7F0;
            --dark: #2D2D2D;
            --accent-warm: #E24A2E;
            --accent-pink: #E9A13B;
            --accent-lavender: #4FB0C6;
            --light-text: #666666;
            --divider: #E5DDD3;
        }

        html, body {
            height: 100%;
            overscroll-behavior: none;
        }

        body {
            font-family: 'Bodoni Moda', serif;
            background: var(--light-bg);
            color: var(--dark);
            -webkit-font-smoothing: antialiased;
            -webkit-tap-highlight-color: transparent;
        }

        .app-container {
            width: 100%;
            min-height: 100dvh;
            background: var(--light-bg);
            overflow: hidden;
            display: flex;
            flex-direction: column;
            position: relative;
            padding-top: env(safe-area-inset-top);
            padding-bottom: env(safe-area-inset-bottom);
        }

        @media (min-width: 640px) {
            .app-container {
                max-width: 480px;
                margin: 0 auto;
                box-shadow: 0 20px 60px rgba(0, 0, 0, 0.08);
            }
        }

        /* Popham Beach watercolor as a full-bleed backdrop behind every
           screen. Washed out under a cream tint (same ~0.12 visible
           strength the old corner badges used) so body text laid directly
           on the background — day-labels, event titles, etc. — stays
           legible. .app-container doesn't scroll (only .screen-content
           does), so this stays put behind scrolling content for free. */
        .app-container {
            background:
                linear-gradient(rgba(245, 240, 235, 0.88), rgba(245, 240, 235, 0.88)),
                url('images/popham.png') center/cover no-repeat,
                var(--light-bg);
        }

        /* Watercolor accent blobs */
        .app-container::after {
            content: '';
            position: absolute;
            width: 250px;
            height: 250px;
            background: radial-gradient(circle at 50% 60%, rgba(155, 126, 181, 0.06), transparent);
            border-radius: 50%;
            bottom: -80px;
            left: -80px;
            pointer-events: none;
        }

        .screen {
            display: none;
            flex-direction: column;
            height: 100%;
            width: 100%;
            position: absolute;
            top: 0;
            left: 0;
            padding-top: 8px;
            overflow-y: auto;
            animation: fadeIn 0.3s ease-in;
            z-index: 1;
        }

        .screen.active {
            display: flex;
        }

        @keyframes fadeIn {
            from { opacity: 0; }
            to { opacity: 1; }
        }

        .screen-content {
            flex: 1;
            padding: 0 24px 100px 24px;
            overflow-y: auto;
        }

        .header {
            padding: 10px 24px 12px 24px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            position: relative;
            z-index: 10;
        }

        .header-title {
            font-family: 'Bodoni Moda', serif;
            font-size: 24px;
            color: var(--dark);
            font-weight: 600;
        }

        .hamburger {
            background: none;
            border: none;
            cursor: pointer;
            padding: 4px;
            padding: 8px;
        }

        /* LOGIN SCREEN */
        .login-screen {
            justify-content: center;
            align-items: center;
            padding: 40px 24px;
        }

        .login-card {
            text-align: center;
        }

        .login-card h1 {
            font-family: 'Bodoni Moda', serif;
            font-size: 48px;
            color: var(--dark);
            margin-bottom: 8px;
            font-weight: 600;
        }

        .login-card p {
            font-family: 'Bodoni Moda', serif;
            font-size: 18px;
            color: var(--light-text);
            margin-bottom: 40px;
        }

        .input-group {
            margin-bottom: 24px;
        }

        /* Match both `input` (no type) and `input[type="text"]`. The build's
           minifier strips redundant `type="text"` attributes, so the bare
           selector is required for the login field to stay styled. */
        input:not([type]),
        input[type="text"] {
            width: 100%;
            padding: 14px 16px;
            border: 1px solid var(--divider);
            border-radius: 8px;
            font-family: 'Nunito', sans-serif;
            font-size: 16px;
            background: #fff;
            color: var(--dark);
        }

        input:not([type])::placeholder,
        input[type="text"]::placeholder {
            color: #999;
            font-family: 'Nunito', sans-serif;
        }

        .helper-text {
            font-family: 'Nunito', sans-serif;
            font-size: 12px;
            color: var(--light-text);
            margin-top: 8px;
        }

        .sr-only {
            position: absolute;
            width: 1px;
            height: 1px;
            padding: 0;
            margin: -1px;
            overflow: hidden;
            clip: rect(0, 0, 0, 0);
            white-space: nowrap;
            border: 0;
        }

        .btn {
            padding: 14px 28px;
            border: none;
            border-radius: 8px;
            font-family: 'Nunito', sans-serif;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s ease;
        }

        .btn-primary {
            background: var(--primary-green);
            color: white;
            width: 100%;
        }

        .btn-primary:hover {
            background: #3d6849;
            color: white;
        }

        .btn-secondary {
            background: transparent;
            color: var(--primary-green);
            border: 1px solid var(--primary-green);
        }

        .btn-secondary:hover {
            background: rgba(74, 124, 89, 0.05);
            color: var(--primary-green);
        }

        /* SPLASH SCREEN */
        .splash-screen {
            justify-content: flex-start;
            align-items: center;
            padding: 48px 24px 32px 24px;
        }

        .splash-content {
            text-align: center;
            width: 100%;
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 0;
        }

        .splash-image {
            width: min(80%, 320px);
            margin: 0 auto 24px auto;
            display: block;
        }

        .splash-image img {
            width: 100%;
            height: auto;
            display: block;
        }

        .splash-text {
            font-family: 'Bodoni Moda', serif;
            font-size: 15px;
            color: var(--dark);
            line-height: 1.7;
            margin-bottom: 32px;
            padding: 0 8px;
            font-style: italic;
        }

        .splash-signature {
            font-family: 'Mea Culpa', cursive;
            font-size: 28px;
            color: var(--dark);
            margin-bottom: 32px;
        }

        .splash-arrow {
            cursor: pointer;
            transition: opacity 0.2s ease;
            margin: 0 auto;
        }

        .splash-arrow:hover {
            opacity: 0.7;
        }

        /* SCHEDULE SCREEN */
        .day-section {
            margin-bottom: 20px;
        }

        .extras-section-title {
            font-family: 'Mea Culpa', cursive;
            font-size: 28px;
            color: var(--primary-green);
            font-weight: 400;
            text-align: center;
            margin: 24px 0 16px 0;
        }

        .day-title {
            font-family: 'Bodoni Moda', serif;
            font-size: 16.5px;
            color: var(--dark);
            font-weight: 400;
            margin-bottom: 4px;
            margin-bottom: 12px;
        }

        .event-card {
            background: white;
            padding: 16px;
            border: 1px solid var(--divider);
            border-radius: 12px;
            cursor: pointer;
            transition: all 0.2s ease;
            margin-bottom: 16px;
            box-shadow: 0 1px 4px rgba(0,0,0,0.06);
        }

        .event-card:hover {
            box-shadow: 0 2px 8px rgba(0,0,0,0.08);
        }

        .event-header {
            display: flex;
            flex-direction: column;
        }

        .venue-row {
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .day-label {
            font-family: 'Bodoni Moda', serif;
            font-size: 15px;
            color: var(--dark);
            font-weight: 400;
            margin-bottom: 6px;
        }

        .event-title {
            font-family: 'Mea Culpa', cursive;
            font-size: 20px;
            color: var(--primary-green);
            margin-bottom: 6px;
            line-height: 1.3;
            font-weight: 600;
        }

        .event-time {
            font-family: 'Bodoni Moda', serif;
            font-size: 15px;
            color: var(--dark);
            margin-bottom: 6px;
        }

        .event-venue {
            font-family: 'Bodoni Moda', serif;
            font-size: 14px;
            color: var(--light-text);
            font-style: italic;
            margin-top: 4px;
        }

        .expand-icon {
            transition: transform 0.2s ease;
            margin-right: 4px;
            display: flex;
            align-items: center;
        }

        .event-card.expanded .expand-icon {
            transform: rotate(180deg);
        }

        .event-details {
            display: none;
            margin-top: 16px;
            padding-top: 16px;
            border-top: 1px solid var(--divider);
        }

        .event-card.expanded .event-details {
            display: block;
        }

        .detail-row {
            margin-bottom: 12px;
        }

        .detail-label {
            font-family: 'Nunito', sans-serif;
            font-size: 12px;
            color: var(--light-text);
            text-transform: uppercase;
            letter-spacing: 1px;
            font-weight: 600;
        }

        .detail-value {
            font-family: 'Bodoni Moda', serif;
            font-size: 14px;
            color: var(--dark);
            margin-top: 4px;
        }

        .detail-note {
            font-family: 'Nunito', sans-serif;
            font-size: 14px;
            color: var(--dark);
            line-height: 1.6;
            margin-top: 4px;
        }

        .link-group {
            display: flex;
            gap: 16px;
            flex-wrap: wrap;
            margin-top: 8px;
        }

        a {
            color: var(--primary-green);
            text-decoration: none;
            font-family: 'Nunito', sans-serif;
            font-size: 13px;
            transition: color 0.2s ease;
        }

        a:hover {
            color: #164E63;
        }

        .action-buttons {
            display: flex;
            gap: 12px;
            margin-top: 16px;
        }

        .btn-small {
            padding: 8px 20px;
            font-size: 13px;
            font-family: 'Nunito', sans-serif;
            font-weight: 600;
            border: 1px solid var(--primary-green);
            color: var(--primary-green);
            border-radius: 20px;
            background: transparent;
            cursor: pointer;
            transition: all 0.2s ease;
        }

        .btn-small:hover {
            background: rgba(74, 124, 89, 0.05);
            color: var(--primary-green);
        }

        a.btn-small {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            text-decoration: none;
        }

        /* LOS INVITADOS SCREEN */
        .avatar-img {
            position: absolute;
            inset: 0;
            width: 100%;
            height: 100%;
            object-fit: cover;
            display: block;
        }

        /* Profile blur-up: thumb sits behind the full image, blurred,
           and the full fades in once it has loaded. */
        .profile-thumb {
            filter: blur(12px);
            transform: scale(1.05);
        }
        .profile-full {
            opacity: 0;
            transition: opacity 0.4s ease;
        }
        .profile-full.loaded {
            opacity: 1;
        }

        .avatar-1 { background: #E24A2E; }
        .avatar-2 { background: #E9A13B; }
        .avatar-3 { background: #4FB0C6; }
        .avatar-4 { background: #1F6E8C; }
        .avatar-5 { background: #D4A574; }

        .invitado-back {
            background: none;
            border: none;
            cursor: pointer;
            padding: 4px;
            display: flex;
            align-items: center;
            justify-content: center;
        }

        .invitado-search-wrap {
            padding: 20px 24px 12px;
        }

        .invitado-search-field {
            position: relative;
        }

        .invitado-search {
            width: 100%;
            padding: 10px 40px 10px 14px;
            border: 1px solid var(--divider);
            border-radius: 6px;
            font-family: 'Nunito', sans-serif;
            font-size: 14px;
            background: white;
            color: var(--dark);
            box-sizing: border-box;
        }

        .invitado-search:focus {
            outline: none;
            border-color: var(--primary-green);
        }

        .invitado-search::placeholder {
            color: var(--light-text);
        }

        .invitado-search-clear {
            position: absolute;
            top: 50%;
            right: 12px;
            transform: translateY(-50%);
            display: flex;
            align-items: center;
            justify-content: center;
            width: 20px;
            height: 20px;
            padding: 0;
            background: #B8B8B8;
            border: none;
            border-radius: 50%;
            color: white;
            cursor: pointer;
            line-height: 0;
        }

        .invitado-search-clear:hover {
            background: #9A9A9A;
        }

        .invitado-search-clear[hidden] {
            display: none;
        }

        .invitado-surprise {
            display: block;
            margin: 0 auto 20px;
            padding: 8px;
            background: none;
            border: none;
            cursor: pointer;
            line-height: 0;
            transition: transform 0.3s ease, opacity 0.2s ease;
        }

        .invitado-surprise svg {
            display: block;
            width: 32px;
            height: 32px;
            /* Flower is drawn pointing up; rotate 90° clockwise so the
               bloom sits on the right and the stem trails to the left. */
            transform: rotate(90deg);
        }

        .invitado-surprise:hover {
            opacity: 0.75;
            transform: rotate(-8deg);
        }

        .invitado-grid {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 16px 12px;
            padding: 0 24px 100px;
        }

        .invitado-thumb {
            cursor: pointer;
            text-align: center;
            display: flex;
            flex-direction: column;
            align-items: center;
        }

        .invitado-thumb-photo {
            width: 100%;
            aspect-ratio: 1;
            border-radius: 8px;
            display: flex;
            align-items: center;
            justify-content: center;
            color: white;
            font-family: 'Nunito', sans-serif;
            font-size: 20px;
            font-weight: 600;
            position: relative;
            overflow: hidden;
            margin-bottom: 8px;
            transition: transform 0.2s ease;
        }

        .invitado-thumb:hover .invitado-thumb-photo {
            transform: scale(1.02);
        }

        .invitado-thumb-name {
            font-family: 'Bodoni Moda', serif;
            font-size: 12px;
            color: var(--dark);
            line-height: 1.3;
        }

        .invitado-empty {
            text-align: center;
            padding: 40px 24px;
            font-family: 'Bodoni Moda', serif;
            font-style: italic;
            font-size: 14px;
            color: var(--light-text);
        }

        /* Linked-partner profile — shown when a chip points at someone
           who hasn't filled the form yet. Just photo + name + this note. */
        .invitado-no-story {
            margin-top: 14px;
            text-align: center;
            font-family: 'Bodoni Moda', serif;
            font-style: italic;
            font-size: 14px;
            color: var(--light-text);
        }
        .invitado-no-story a {
            color: var(--accent-warm);
            text-decoration: underline;
        }

        .invitado-profile {
            padding: 0 24px 100px;
        }

        /* Who's Coming screen title — override the default Bodoni
           header style for this one screen with a Mea Culpa flourish. */
        .invitado-screen-title {
            font-family: 'Mea Culpa', cursive;
            font-weight: 400;
            font-size: 36px;
            color: var(--primary-green);
            line-height: 1;
        }

        .invitado-card {
            text-align: center;
            user-select: none;
            -webkit-user-select: none;
            touch-action: pan-y;
        }

        .invitado-photo {
            width: 100%;
            max-width: 320px;
            aspect-ratio: 1;
            margin: 0 auto 20px;
            border-radius: 12px;
            display: flex;
            align-items: center;
            justify-content: center;
            color: white;
            font-family: 'Nunito', sans-serif;
            font-size: 64px;
            font-weight: 600;
            position: relative;
            overflow: hidden;
        }

        .invitado-name {
            font-family: 'Mea Culpa', cursive;
            font-size: 32px;
            color: var(--primary-green);
            line-height: 1.1;
            margin-bottom: 6px;
        }

        .invitado-city {
            font-family: 'Bodoni Moda', serif;
            font-size: 13px;
            font-style: italic;
            color: var(--light-text);
            margin-bottom: 16px;
        }

        .invitado-hometown {
            font-family: 'Bodoni Moda', serif;
            font-size: 13px;
            font-style: italic;
            color: var(--light-text);
            margin-top: -10px;
            margin-bottom: 16px;
        }

        .invitado-story-label,
        .invitado-least-label,
        .invitado-memory-label {
            font-family: 'Lateef', serif;
            font-weight: 700;
            font-size: 24px;
            color: var(--primary-green);
            text-align: center;
            line-height: 1.1;
            margin-top: 20px;
            margin-bottom: 2px;
        }

        .invitado-story,
        .invitado-least,
        .invitado-memory {
            font-family: 'Bodoni Moda', serif;
            font-weight: 400;
            font-size: 15px;
            color: var(--dark);
            line-height: 1.5;
            text-align: center;
            white-space: pre-wrap;
            margin-bottom: 8px;
        }

        .invitado-herewith {
            font-family: 'Bodoni Moda', serif;
            font-weight: 400;
            font-size: 15px;
            color: var(--dark);
            line-height: 1.5;
            text-align: center;
            margin-top: 4px;
            margin-bottom: 16px;
        }

        .invitado-herewith-link {
            background: none;
            border: none;
            padding: 0;
            font: inherit;
            color: var(--primary-green);
            cursor: pointer;
        }

        .invitado-herewith-link:hover {
            text-decoration: underline;
        }

        /* Stay in Touch — post-wedding contact info shared via the
           "Contact & Socials" form. Sits AFTER the wedding-narrative
           sections (story / memories / events) because it's the
           future-tense block: where to find this person once everyone
           has gone home. Only renders when the guest has at least one
           non-empty contact field. */
        .invitado-contact-label {
            font-family: 'Lateef', serif;
            font-weight: 700;
            font-size: 24px;
            color: var(--primary-green);
            text-align: center;
            line-height: 1.1;
            margin-top: 24px;
            margin-bottom: 8px;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            width: 100%;
        }
        .invitado-contact-label-bud {
            /* Sized to read at the same optical weight as the Lateef
               capital next to it, no rotation (the surprise-button
               rotates 90° to "trail the stem" — at this size that
               crops awkwardly). */
            width: 18px;
            height: 18px;
            display: inline-block;
            vertical-align: middle;
        }
        .invitado-contacts {
            margin: 4px 0 0;
            padding: 0;
            list-style: none;
            display: flex;
            flex-direction: column;
            gap: 6px;
            align-items: center;
        }
        .invitado-contacts li {
            display: inline-flex;
            align-items: center;
            gap: 10px;
            font-family: 'Bodoni Moda', serif;
            font-size: 14px;
            line-height: 1.4;
            max-width: 100%;
        }
        .invitado-contacts a {
            color: var(--primary-green);
            text-decoration: none;
            border-bottom: 1px solid transparent;
            transition: border-color 0.15s ease;
            /* Long emails / LinkedIn URLs can overflow on narrow
               viewports — let them break gracefully. */
            word-break: break-word;
        }
        .invitado-contacts a:hover,
        .invitado-contacts a:active {
            border-bottom-color: var(--primary-green);
        }
        .invitado-contact-icon {
            width: 18px;
            height: 18px;
            flex: 0 0 18px;
            color: var(--light-text);
            display: inline-flex;
            align-items: center;
            justify-content: center;
        }
        .invitado-contact-icon svg {
            width: 100%;
            height: 100%;
        }

        /* The matching badge on the grid thumb — a tiny rosa rugosa
           bud sitting in a cream chip, bottom-right corner. Same source
           of truth as the profile section: hasContacts → true means
           both the badge AND the section render. */
        .invitado-thumb-badge {
            position: absolute;
            bottom: 4px;
            right: 4px;
            width: 22px;
            height: 22px;
            background: #FBF6EE;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            box-shadow: 0 1px 3px rgba(0, 0, 0, 0.18);
            z-index: 2;
            pointer-events: none;
        }
        .invitado-thumb-badge img {
            width: 15px;
            height: 15px;
            display: block;
        }

        /* Events — deliberately the quietest section on the card.
           Sits at the bottom: a plain "Events" label above a stacked
           list of the events the guest has RSVPed to, one per line in
           italic Bodoni. */
        .invitado-events {
            margin-top: 28px;
            text-align: center;
        }
        .invitado-events-label {
            font-family: 'Bodoni Moda', serif;
            font-size: 11px;
            color: var(--light-text);
            margin-bottom: 4px;
        }
        .invitado-events-body {
            font-family: 'Bodoni Moda', serif;
            font-size: 13px;
            font-style: italic;
            color: var(--dark);
        }
        .invitado-events-item {
            line-height: 1.5;
        }

        /* "Claim profile" footer — tiny, subtle SMS link so guests can
           text Elien to request an edit to their profile. Even quieter
           than the events block. */
        .invitado-claim {
            margin-top: 24px;
            text-align: center;
        }
        .invitado-claim a {
            font-family: 'Nunito', sans-serif;
            font-size: 10px;
            color: var(--light-text);
            text-decoration: underline;
            text-decoration-color: rgba(0, 0, 0, 0.15);
            text-underline-offset: 2px;
        }
        .invitado-claim a:hover,
        .invitado-claim a:focus {
            color: var(--primary-green);
            text-decoration-color: var(--primary-green);
        }

        .invitado-nav {
            display: flex;
            justify-content: center;
            align-items: center;
            gap: 64px;
            margin-top: 16px;
        }

        .invitado-arrow {
            background: none;
            border: none;
            cursor: pointer;
            padding: 8px;
            color: var(--primary-green);
            transition: opacity 0.2s ease;
            display: flex;
            align-items: center;
            justify-content: center;
        }

        .invitado-arrow:hover {
            opacity: 0.6;
        }

        /* TOASTS SCREEN — night-split, expandable cards (RTL Persian text). */
        .toast-day {
            font-family: 'Mea Culpa', cursive;
            font-size: 28px;
            color: var(--primary-green);
            margin: 8px 0 12px 0;
            font-weight: 400;
        }

        .toast-day + .toast-day,
        .toast-card + .toast-day {
            margin-top: 24px;
        }

        .toast-card {
            background: white;
            padding: 16px;
            border-radius: 8px;
            margin-bottom: 12px;
            border: 1px solid var(--divider);
            cursor: pointer;
            transition: background 0.2s ease;
        }

        .toast-card:hover {
            background: rgba(74, 124, 89, 0.02);
        }

        .toast-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 12px;
        }

        .toast-speaker {
            font-family: 'Bodoni Moda', serif;
            font-size: 16px;
            color: var(--dark);
            font-weight: 600;
        }

        .toast-toggle {
            color: var(--light-text);
            font-size: 16px;
            transition: transform 0.2s ease;
            flex-shrink: 0;
        }

        .toast-card.open .toast-toggle {
            transform: rotate(180deg);
        }

        .toast-body {
            display: none;
            padding-top: 12px;
            margin-top: 12px;
            border-top: 1px solid var(--divider);
        }

        .toast-card.open .toast-body {
            display: block;
        }

        .toast-text {
            font-family: 'Bodoni Moda', serif;
            font-size: 15px; /* +12% from 13 — readability pass */
            color: var(--dark);
            line-height: 1.8;
            direction: rtl;
            text-align: right;
        }

        .toast-text p {
            margin: 0 0 12px 0;
        }

        .toast-text p:last-child {
            margin-bottom: 0;
        }

        /* Audio-first layout: player renders above the Persian text so
           listening is the primary affordance. The toast-body itself
           carries the top divider (separating the body from the speaker
           header), so the player needs no top border of its own. The
           divider between player and text is on `.audio-player +
           .toast-text` below. */
        .audio-player {
            display: flex;
            align-items: center;
            gap: 12px;
        }

        .audio-player + .toast-text {
            padding-top: 12px;
            margin-top: 12px;
            border-top: 1px solid var(--divider);
        }

        .play-btn {
            width: 36px;
            height: 36px;
            border-radius: 50%;
            background: var(--accent-warm);
            border: none;
            color: white;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 14px;
            line-height: 1;
            transition: background 0.2s ease;
            flex-shrink: 0;
            padding: 0;
        }

        .play-btn:hover {
            background: #d6734d;
        }

        .progress-bar {
            flex: 1;
            height: 4px;
            background: var(--divider);
            border-radius: 2px;
            cursor: pointer;
            position: relative;
            overflow: hidden;
        }

        .progress-fill {
            position: absolute;
            top: 0;
            left: 0;
            bottom: 0;
            width: 0%;
            background: var(--accent-warm);
            border-radius: 2px;
            pointer-events: none;
        }

        .duration {
            font-family: 'Nunito', sans-serif;
            font-size: 12px;
            color: var(--light-text);
            min-width: 30px;
            text-align: right;
            font-variant-numeric: tabular-nums;
        }

        /* SAN MIGUEL GUIDE SCREEN
           Layout: italic intro → pill jump-nav → stacked sections.
           Each section has an h3 heading, optional pinned Google-Maps
           shortcuts, then a list of Travel-style collapsible place-cards. */
        .guide-intro {
            font-family: 'Bodoni Moda', serif;
            font-size: 16px; /* +12% from 14 — readability pass */
            color: var(--dark);
            line-height: 1.6;
            font-style: italic;
            margin-bottom: 18px;
        }

        .guide-jump-nav {
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            margin: 0 0 22px;
        }

        .guide-jump-pill {
            font-family: 'Bodoni Moda', serif;
            font-size: 15px; /* +12% from 13 — readability pass per Elien's note */
            color: white;
            background: #7FB5C9;
            border: none;
            border-radius: 999px;
            padding: 7px 15px;
            cursor: pointer;
            transition: background 0.2s ease;
            white-space: nowrap;
        }
        .guide-jump-pill:hover,
        .guide-jump-pill:active {
            background: var(--primary-green);
        }

        .guide-section {
            margin: 0 0 28px;
        }
        .guide-section:last-of-type { margin-bottom: 8px; }

        .guide-section-title {
            font-family: 'Bodoni Moda', serif;
            font-size: 20px; /* +12% from 18 — readability pass per Elien's note */
            font-weight: 600;
            color: var(--dark);
            margin: 0 0 10px;
        }

        /* Section title doubles as a collapse toggle. The whole row is the
           click target; arrow on the right rotates when collapsed and the
           body (maps + cards) hides. Default state is expanded. */
        .guide-section-toggle {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 10px;
            width: 100%;
            background: none;
            border: none;
            padding: 0;
            margin: 0;
            color: inherit;
            font: inherit;
            text-align: left;
            cursor: pointer;
        }
        /* Hand-drawn brush-stroke chevron — same SVG and interaction as the
           schedule page's .expand-icon. Default state is expanded so the
           arrow points up; collapsing rotates back down. */
        .guide-section-arrow {
            display: flex;
            align-items: center;
            transition: transform 0.2s ease;
            transform: rotate(180deg);
            flex-shrink: 0;
        }
        .guide-section.collapsed .guide-section-arrow {
            transform: rotate(0deg);
        }
        .guide-section.collapsed .guide-section-body {
            display: none;
        }

        .guide-section-maps {
            display: flex;
            flex-direction: column;
            gap: 4px;
            margin: 0 0 14px;
        }
        /* Bare-text link — pin icon, label, arrow. No pill background. */
        .guide-map-link {
            font-family: 'Bodoni Moda', serif;
            font-size: 16px; /* +12% from 14 — readability pass per Elien's note */
            color: var(--primary-green);
            text-decoration: none;
            display: inline-flex;
            align-items: center;
            gap: 8px;
            line-height: 1.4;
            padding: 4px 0;
        }
        .guide-map-link:hover {
            color: var(--deep-green);
        }
        .guide-map-icon {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            width: 16px;
            height: 16px;
            color: var(--primary-green);
            flex-shrink: 0;
        }
        .guide-section-cards {
            display: flex;
            flex-direction: column;
            gap: 10px;
        }

        /* "For a Good Time" screen — illustrated trio. Each S-word is its
           own editorial unit: small line-art icon, script S-name, body. */
        .goodtime-content {
            max-width: 340px;
            margin: 12px auto 0;
            padding: 0 8px;
        }
        .goodtime-heading {
            font-family: 'Mea Culpa', cursive;
            /* Match the Who's Coming screen title (.invitado-screen-title). */
            font-size: 36px;
            font-weight: 400;
            color: var(--primary-green);
            text-align: center;
            margin: 8px 0 40px;
            line-height: 1;
        }
        .goodtime-tip {
            display: flex;
            flex-direction: column;
            align-items: center;
            text-align: center;
            margin-bottom: 36px;
        }
        .goodtime-tip:last-child { margin-bottom: 0; }
        .goodtime-tip-icon {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 10px;
            min-height: 48px;
            color: var(--primary-green);
            margin-bottom: 6px;
        }
        .goodtime-tip-icon svg {
            width: 44px;
            height: 44px;
            stroke: currentColor;
            fill: none;
            stroke-linecap: round;
            stroke-linejoin: round;
        }
        /* Black-line PNGs from the Noun Project tinted to the primary green
           via filter — converts black pixels to a green close to var(--primary-green: #1F6E8C).
           See: https://codepen.io/sosuke/pen/Pjoqqp for the multi-step filter recipe. */
        .goodtime-tip-icon img.goodtime-tip-img {
            width: 48px;
            height: 48px;
            object-fit: contain;
            filter: brightness(0) saturate(100%)
                    invert(45%) sepia(15%) saturate(800%) hue-rotate(85deg) brightness(92%) contrast(85%);
        }
        .goodtime-tip-name {
            font-family: 'Lateef', serif;
            font-size: 32px;
            font-weight: 700;
            color: var(--primary-green);
            line-height: 1.05;
            margin-bottom: 8px;
        }
        .goodtime-tip-body {
            font-family: 'Bodoni Moda', serif;
            font-size: 16px; /* +12% from 14 — readability pass */
            color: var(--dark);
            line-height: 1.6;
            font-style: italic;
            max-width: 320px;
        }
        .goodtime-link {
            font-family: 'Bodoni Moda', serif;
            font-size: inherit;
            color: var(--primary-green);
            text-decoration: underline;
            text-decoration-thickness: 1px;
            text-underline-offset: 2px;
            font-style: italic;
        }
        .goodtime-link:hover { color: var(--deep-green); }

        /* Travel-style collapsible place-card. Header surfaces name +
           address; tapping expands the body for the description and
           Get Directions. Static variant (.place-card-header--static)
           drops the arrow when there's no body to expand. */
        .place-card {
            background: white;
            border: 1px solid var(--divider);
            border-radius: 12px;
            overflow: hidden;
        }
        .place-card-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 10px;
            padding: 14px 16px;
            cursor: pointer;
        }
        .place-card-header--static { cursor: default; }
        .place-card-summary { flex: 1; min-width: 0; }
        .place-card-arrow {
            color: var(--light-text);
            font-size: 14px;
            transition: transform 0.2s ease;
            flex-shrink: 0;
        }
        .place-card.expanded .place-card-arrow {
            transform: rotate(180deg);
        }
        .place-card-body {
            display: none;
            padding: 0 16px 14px;
            border-top: 1px solid var(--divider);
        }
        .place-card.expanded .place-card-body {
            display: block;
            padding-top: 12px;
        }

        .place-name {
            /* Lateef gives the names a more grounded, editorial feel than
               Mea Culpa's calligraphy — better at small sizes too. */
            font-family: 'Lateef', serif;
            font-size: 28px;
            font-weight: 700;
            color: var(--primary-green);
            line-height: 1.05;
            margin-bottom: 4px;
        }

        /* Rooftop indicator next to the place name. Toggled per-place via
           the editor's Rooftop checkbox. Burnt-orange to read at a glance. */
        .place-rooftop {
            display: inline-block;
            margin-left: 6px;
            vertical-align: 1px;
            color: var(--accent-warm);
        }
        .place-rooftop svg {
            width: 16px;
            height: 16px;
            stroke: currentColor;
        }

        .place-address {
            font-family: 'Nunito', sans-serif;
            font-size: 16px; /* +12% from 14 — readability pass */
            color: var(--light-text);
            line-height: 1.45;
            margin: 0 0 10px;
        }

        .place-note {
            font-family: 'Bodoni Moda', serif;
            font-size: 16px; /* +12% from 14 — readability pass */
            color: var(--dark);
            line-height: 1.55;
            font-style: italic;
            margin: 4px 0 0;
        }
        /* Description preview: when the card is collapsed, clamp the
           description to two lines so the card stays compact. Tapping the
           arrow expands and the full description shows. */
        .place-card.collapsible:not(.expanded) .place-card-summary .place-note {
            display: -webkit-box;
            -webkit-line-clamp: 2;
            -webkit-box-orient: vertical;
            overflow: hidden;
        }

        .place-directions {
            display: inline-block;
            color: var(--primary-green);
            text-decoration: none;
            font-family: 'Nunito', sans-serif;
            font-size: 16px; /* +12% from 14 — readability pass */
        }
        .place-directions:hover { color: var(--deep-green); }

        /* Shopping place-card photo. Lives in the open state of the card
           — collapsed view never reveals it. 3:2 ratio matches the crop
           the editor pipeline produces (images/guide/<key>.webp), so the
           rendered frame is always filled edge-to-edge with no letterbox. */
        .place-photo {
            margin: 0 0 12px;
            border-radius: 10px;
            overflow: hidden;
            background: #fafafa;
        }
        .place-photo picture,
        .place-photo img {
            display: block;
            width: 100%;
            aspect-ratio: 3 / 2;
            object-fit: cover;
        }

        /* FAQ SCREEN */
        .accordion-item {
            border-bottom: 1px solid var(--divider);
            margin-bottom: 0;
        }

        .accordion-header {
            padding: 16px 0;
            cursor: pointer;
            display: flex;
            justify-content: space-between;
            align-items: center;
            transition: background 0.2s ease;
        }

        .accordion-header:hover {
            background: rgba(74, 124, 89, 0.02);
        }

        .accordion-question {
            font-family: 'Bodoni Moda', serif;
            font-size: 14px;
            color: var(--dark);
            font-weight: 600;
        }

        .accordion-toggle {
            color: var(--light-text);
            font-size: 18px;
            transition: transform 0.2s ease;
        }

        .accordion-item.open .accordion-toggle {
            transform: rotate(180deg);
        }

        .accordion-content {
            display: none;
            padding: 0 0 16px 0;
            font-family: 'Bodoni Moda', serif;
            font-size: 13px;
            color: var(--dark);
            line-height: 1.6;
        }

        .accordion-item.open .accordion-content {
            display: block;
        }

        /* ALBUM SCREEN */
        .album-card {
            text-align: center;
            padding: 40px 24px;
        }

        .album-title {
            font-family: 'Bodoni Moda', serif;
            font-size: 24px;
            color: var(--dark);
            margin-bottom: 12px;
            font-weight: 600;
        }

        .album-note {
            font-family: 'Bodoni Moda', serif;
            font-size: 14px;
            color: var(--light-text);
            line-height: 1.6;
            margin-bottom: 24px;
        }

        /* BOTTOM TAB BAR */
        .tab-bar {
            position: absolute;
            bottom: 0;
            width: 100%;
            background: white;
            border-top: 1px solid var(--divider);
            display: none;
            justify-content: center;
            gap: 12px;
            padding: 0 40px;
            height: 80px;
            z-index: 100;
            touch-action: manipulation;
        }

        .tab {
            flex: 1;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            cursor: pointer;
            border: none;
            background: none;
            gap: 4px;
            transition: all 0.2s ease;
            color: var(--light-text);
            touch-action: manipulation;
        }

        .tab.active {
            color: var(--accent-warm);
        }

        .tab-icon {
            font-size: 24px;
            display: flex;
            align-items: center;
            justify-content: center;
        }

        .tab-icon svg {
            width: 28px;
            height: 28px;
        }

        .tab-icon svg path, .tab-icon svg rect, .tab-icon svg circle, .tab-icon svg line, .tab-icon svg polyline, .tab-icon svg ellipse, .tab-icon svg text {
            stroke: #7FB5C9;
            transition: stroke 0.2s ease, fill 0.2s ease;
        }

        .tab-icon svg circle.dot, .tab-icon svg .fill-elem {
            fill: #7FB5C9;
            stroke: none;
        }

        .tab-icon svg text {
            fill: #7FB5C9;
            stroke: none;
        }

        .tab.active .tab-icon svg path, .tab.active .tab-icon svg rect, .tab.active .tab-icon svg circle, .tab.active .tab-icon svg line, .tab.active .tab-icon svg polyline, .tab.active .tab-icon svg ellipse, .tab.active .tab-icon svg text {
            stroke: var(--accent-warm);
        }

        .tab.active .tab-icon svg circle.dot, .tab.active .tab-icon svg .fill-elem {
            fill: var(--accent-warm);
            stroke: none;
        }

        .tab.active .tab-icon svg text {
            fill: var(--accent-warm);
            stroke: none;
        }


        .tab-label {
            display: none;
        }

        /* INSTALL TO HOME SCREEN — floating CTA on login, menu item afterward,
           bottom-sheet instructions for iOS Safari (no native prompt). */
        .install-cta {
            position: fixed;
            left: 50%;
            bottom: 32px;
            transform: translateX(-50%);
            background: var(--primary-green);
            color: white;
            border: none;
            border-radius: 999px;
            padding: 12px 22px;
            font-family: 'Nunito', sans-serif;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            box-shadow: 0 6px 20px rgba(74, 124, 89, 0.28);
            z-index: 500;
            display: none;
            align-items: center;
            gap: 8px;
            white-space: nowrap;
        }

        .install-cta.visible { display: inline-flex; }
        .install-cta svg { display: block; }

        .install-menu-item { display: none; }
        .install-menu-item.visible { display: block; }

        .install-overlay {
            display: none;
            position: fixed;
            inset: 0;
            background: rgba(0, 0, 0, 0.45);
            z-index: 1500;
            align-items: flex-end;
            justify-content: center;
        }
        .install-overlay.active { display: flex; }

        .install-sheet {
            background: white;
            border-radius: 16px 16px 0 0;
            padding: 24px 22px 32px;
            width: 100%;
            max-width: 480px;
            color: var(--dark);
            box-shadow: 0 -10px 30px rgba(0, 0, 0, 0.18);
        }

        .install-sheet h2 {
            font-family: 'Bodoni Moda', serif;
            font-size: 22px;
            font-weight: 600;
            margin: 0 0 12px;
            color: var(--dark);
        }

        .install-sheet p,
        .install-sheet ol {
            font-family: 'Nunito', sans-serif;
            font-size: 14px;
            line-height: 1.6;
            color: var(--dark);
        }

        .install-sheet p { margin: 0 0 14px; }
        .install-sheet ol { padding-left: 20px; margin: 0 0 18px; }
        .install-sheet ol li { margin-bottom: 6px; }
        .install-sheet .share-glyph {
            display: inline-block;
            vertical-align: -3px;
            margin: 0 2px;
        }

        .install-sheet-close {
            background: none;
            border: 1px solid var(--divider);
            border-radius: 8px;
            padding: 12px 14px;
            font-family: 'Nunito', sans-serif;
            font-size: 14px;
            color: var(--dark);
            cursor: pointer;
            width: 100%;
        }

        /* HAMBURGER MENU */
        .menu-overlay {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: rgba(0, 0, 0, 0.3);
            z-index: 999;
        }

        .menu-overlay.active {
            display: block;
        }

        .menu-drawer {
            position: fixed;
            top: 0;
            left: -280px;
            width: 280px;
            height: 100%;
            background: white;
            z-index: 1000;
            transition: left 0.3s ease;
            padding-top: 60px;
        }

        .menu-drawer.active {
            left: 0;
        }

        .menu-item {
            padding: 16px 24px;
            border-bottom: 1px solid var(--divider);
            cursor: pointer;
            font-family: 'Bodoni Moda', serif;
            font-size: 16px;
            color: var(--dark);
            transition: background 0.2s ease;
            background: none;
            border: none;
            width: 100%;
            text-align: left;
        }

        .menu-item:hover {
            background: rgba(74, 124, 89, 0.05);
        }

        /* Sun icon next to "For a Good Time" only — visual cue that this
           is the editorial / vibe page, not a navigational utility.
           Sun sits immediately to the right of the label, separated only
           by the flex gap, so the pair reads as one unit. */
        .menu-item--good-time {
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .menu-item-icon {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            width: 18px;
            height: 18px;
            color: var(--accent-warm);
            flex-shrink: 0;
        }

        .menu-item-icon svg {
            width: 100%;
            height: 100%;
        }

        /* Build hash, pinned to the bottom-left of the drawer. Lets us
           confirm an installed PWA actually picked up the latest deploy
           — if the hash here matches the CACHE_VERSION in the deployed
           sw.js, the new bundle is live. Hidden from screen readers. */
        .menu-version {
            position: absolute;
            left: 24px;
            bottom: calc(16px + env(safe-area-inset-bottom));
            font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
            font-size: 12px;
            color: #b4ada4;
            letter-spacing: 0.02em;
            user-select: all;
        }

        /* COFFEE MAP & TRAVEL INFO */
        .info-section {
            margin-bottom: 24px;
        }

        .info-title {
            font-family: 'Bodoni Moda', serif;
            font-size: 16px;
            color: var(--dark);
            font-weight: 600;
            margin-bottom: 12px;
        }

        .info-item {
            padding: 8px 0;
            font-family: 'Bodoni Moda', serif;
            font-size: 13px;
            color: var(--dark);
            line-height: 1.6;
        }

        .coffee-map-placeholder {
            width: 100%;
            aspect-ratio: 4/3;
            border: 2px dashed var(--divider);
            border-radius: 8px;
            display: flex;
            align-items: center;
            justify-content: center;
            background: rgba(212, 231, 208, 0.2);
            color: var(--light-text);
            font-family: 'Nunito', sans-serif;
            font-size: 13px;
            margin-bottom: 24px;
        }

        .airport-item {
            background: white;
            padding: 12px;
            border-radius: 6px;
            margin-bottom: 8px;
            border: 1px solid var(--divider);
        }

        .airport-code {
            font-family: 'Bodoni Moda', serif;
            font-size: 14px;
            color: var(--dark);
            font-weight: 600;
        }

        .airport-name {
            font-family: 'Bodoni Moda', serif;
            font-size: 12px;
            color: var(--light-text);
            margin-top: 2px;
        }

        /* Scrollbar styling */
        .screen-content::-webkit-scrollbar {
            width: 4px;
        }

        .screen-content::-webkit-scrollbar-track {
            background: transparent;
        }

        .screen-content::-webkit-scrollbar-thumb {
            background: var(--divider);
            border-radius: 2px;
        }

        /* PWA update indicator: shown only while a new service worker
           is installing on top of an already-controlled page. */
        .update-indicator {
            position: fixed;
            inset: 0;
            background: rgba(252, 248, 240, 0.94);
            -webkit-backdrop-filter: blur(8px);
            backdrop-filter: blur(8px);
            display: none;
            align-items: center;
            justify-content: center;
            z-index: 9999;
        }
        .update-indicator.visible {
            display: flex;
        }
        .update-flower {
            width: 140px;
            height: 140px;
            object-fit: contain;
            animation: update-flower-breathe 2.4s ease-in-out infinite;
            transform-origin: center;
        }
        @keyframes update-flower-breathe {
            0%, 100% { transform: scale(0.78); }
            50% { transform: scale(1.08); }
        }
        @media (prefers-reduced-motion: reduce) {
            .update-flower {
                animation: update-flower-pulse 2.4s ease-in-out infinite;
            }
            @keyframes update-flower-pulse {
                0%, 100% { opacity: 0.55; }
                50% { opacity: 1; }
            }
        }
    </style>
</head>
<body>
    <div class="update-indicator" id="updateIndicator" role="status" aria-live="polite" aria-label="Updating app">
        <svg class="update-flower" viewBox="0 0 48 48" role="presentation" aria-hidden="true">
            <path d="M20 34 L18 12 L30 12 L28 34 Z" fill="#F5F0EB" stroke="#1F6E8C" stroke-width="2"/>
            <rect x="17" y="20" width="14" height="6" fill="#E24A2E"/>
            <rect x="19" y="6" width="10" height="8" fill="#1F6E8C"/>
            <path d="M17 6 L24 0 L31 6 Z" fill="#E24A2E"/>
        </svg>
    </div>
    <div class="app-container">
            <!-- LOGIN SCREEN -->
            <div class="screen login-screen active" id="login">
                <div class="login-card">
                    <h1 style="font-family: 'Mea Culpa', cursive; font-size: 56px; font-weight: 400;">Hilary & Elliott</h1>
                    <p>Wiscasset, Maine</p>
                    <form id="loginForm" onsubmit="event.preventDefault(); handleLogin();" novalidate>
                        <div class="input-group">
                            <label for="nameInput" class="sr-only">Full name</label>
                            <input
                                type="text"
                                id="nameInput"
                                name="name"
                                autocomplete="name"
                                autocapitalize="words"
                                autocorrect="off"
                                spellcheck="false"
                                enterkeyhint="go"
                                placeholder="Your full name"
                                aria-describedby="nameHelp"
                                required>
                            <div class="helper-text" id="nameHelp">Enter your full name</div>
                        </div>
                        <button type="submit" aria-label="Continue" style="background: none; border: none; cursor: pointer; margin-top: 16px; display: flex; align-items: center; justify-content: center; margin-left: auto; margin-right: auto;">
                            <svg width="40" height="40" viewBox="0 0 40 40" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true" focusable="false">
                                <path d="M12 10 C16 16, 26 17, 32 20 C26 23, 16 24, 12 30" stroke="#1F6E8C" stroke-width="2.5" fill="none" stroke-linecap="round"/>
                            </svg>
                        </button>
                    </form>
                </div>
            </div>

            <!-- SPLASH SCREEN -->
            <div class="screen splash-screen" id="splash">
                <div class="splash-content">
                    <div class="splash-image">
                        <svg viewBox="0 0 320 240" role="img" aria-label="A lighthouse on the Maine coast">
                            <rect x="0" y="0" width="320" height="240" rx="16" fill="#F5F0EB"/>
                            <circle cx="160" cy="88" r="46" fill="#E24A2E"/>
                            <g stroke="#E24A2E" stroke-width="6" stroke-linecap="round">
                                <line x1="160" y1="14" x2="160" y2="30"/>
                                <line x1="160" y1="146" x2="160" y2="130"/>
                                <line x1="86" y1="88" x2="102" y2="88"/>
                                <line x1="218" y1="88" x2="234" y2="88"/>
                                <line x1="108" y1="36" x2="118" y2="46"/>
                                <line x1="212" y1="36" x2="202" y2="46"/>
                                <line x1="108" y1="140" x2="118" y2="130"/>
                                <line x1="212" y1="140" x2="202" y2="130"/>
                            </g>
                            <path d="M0 170 Q80 158 160 170 T320 170 V240 H0 Z" fill="#1F6E8C"/>
                            <path d="M0 190 Q80 180 160 190 T320 190" stroke="#F5F0EB" stroke-width="4" fill="none" opacity="0.5"/>
                            <path d="M0 210 Q80 200 160 210 T320 210" stroke="#F5F0EB" stroke-width="4" fill="none" opacity="0.35"/>
                            <path d="M190 240 L200 150 L245 130 L280 150 L300 240 Z" fill="#123047"/>
                            <path d="M232 150 L226 60 L254 60 L248 150 Z" fill="#F5F0EB" stroke="#123047" stroke-width="3"/>
                            <rect x="226" y="80" width="28" height="14" fill="#E24A2E"/>
                            <rect x="226" y="110" width="28" height="14" fill="#E24A2E"/>
                            <rect x="230" y="42" width="20" height="18" fill="#123047"/>
                            <path d="M228 42 L240 24 L252 42 Z" fill="#E24A2E"/>
                            <path d="M240 50 L150 20 L150 34 Z" fill="#FBDE9C" opacity="0.85"/>
                            <path d="M60 70 Q66 62 72 70 Q78 62 84 70" stroke="#1F6E8C" stroke-width="3" fill="none" stroke-linecap="round"/>
                            <path d="M90 50 Q95 44 100 50 Q105 44 110 50" stroke="#1F6E8C" stroke-width="3" fill="none" stroke-linecap="round"/>
                        </svg>
                    </div>
                    <div class="splash-text">
                        We are so, so excited that you are joining us for our commitment celebration! Some of the things that drew us together most early on were how important community is and our shared understanding that a best friend is in fact a tier not a person. While we are choosing not to get legally married, we couldn&rsquo;t pass up an opportunity to bring the people who mean the most to us to one of our favorite states to eat, dance, yap, dance some more, yap some more, etc. We hope you have an absolute blast and are so grateful to you for making the schlep!
                    </div>
                    <div class="splash-signature">Love, Hilary & Elliott</div>
                    <div class="splash-arrow" onclick="skipSplash()">
                        <svg width="40" height="40" viewBox="0 0 40 40" fill="none">
                            <path d="M12 10 C16 16, 26 17, 32 20 C26 23, 16 24, 12 30" stroke="#1F6E8C" stroke-width="2.5" fill="none" stroke-linecap="round"/>
                        </svg>
                    </div>
                </div>
            </div>

            <!-- SCHEDULE SCREEN -->
            <div class="screen" id="schedule">
                <div class="header">
                    <h1 class="header-title">Schedule of Events</h1>
                    <button class="hamburger" onclick="toggleMenu()"><svg width="22" height="16" viewBox="0 0 22 16" fill="none"><line x1="1" y1="1" x2="21" y2="1" stroke="#1F6E8C" stroke-width="1.5" stroke-linecap="round"/><line x1="1" y1="8" x2="21" y2="8" stroke="#1F6E8C" stroke-width="1.5" stroke-linecap="round"/><line x1="1" y1="15" x2="21" y2="15" stroke="#1F6E8C" stroke-width="1.5" stroke-linecap="round"/></svg></button>
                </div>
                <div class="screen-content">
{{EVENTS}}

                    <h3 class="extras-section-title">Extra Activities</h3>
{{EXTRAS}}

                    <div style="text-align: center; padding: 32px 0 48px;">
                        <span style="font-family: 'Mea Culpa', cursive; font-size: 20px; color: var(--primary-green);">Thank you for being here!</span>
                    </div>
                </div>
            </div>

            <!-- EXTRA ACTIVITIES SCREEN (hamburger menu) -->
            <!-- Mirrors the Schedule screen markup so the .event-card
                 styling and toggleEvent() expand-on-tap behavior apply
                 for free. Activities live in wedding.db's `extras`
                 table, separate from the formal `events` table. -->
            <div class="screen" id="extras">
                <div class="header">
                    <h1 class="header-title">Extra Activities</h1>
                    <button class="hamburger" onclick="toggleMenu()"><svg width="22" height="16" viewBox="0 0 22 16" fill="none"><line x1="1" y1="1" x2="21" y2="1" stroke="#1F6E8C" stroke-width="1.5" stroke-linecap="round"/><line x1="1" y1="8" x2="21" y2="8" stroke="#1F6E8C" stroke-width="1.5" stroke-linecap="round"/><line x1="1" y1="15" x2="21" y2="15" stroke="#1F6E8C" stroke-width="1.5" stroke-linecap="round"/></svg></button>
                </div>
                <div class="screen-content">
{{EXTRAS}}
                </div>
            </div>

            <!-- LOS INVITADOS SCREEN -->
            <div class="screen" id="facebook">
                <div class="header">
                    <button class="invitado-back" id="invitadoBackBtn" onclick="invitadoBack()" aria-label="Back" style="display:none;">
                        <!-- Stylized back chevron: stroked with rounded caps for a
                             drawn-but-precise feel. Shown only when a profile is
                             open (returns to the browse grid); hidden on the
                             browse grid itself, since Who's Coming is the app's
                             home. invitadoShowProfile / invitadoShowBrowse toggle
                             the inline display style. -->
                        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                            <path d="M14 6 L8 12 L14 18" stroke="#1F6E8C" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>
                        </svg>
                    </button>
                    <h1 class="header-title invitado-screen-title">Who's Coming</h1>
                    <button class="hamburger" onclick="toggleMenu()"><svg width="22" height="16" viewBox="0 0 22 16" fill="none"><line x1="1" y1="1" x2="21" y2="1" stroke="#1F6E8C" stroke-width="1.5" stroke-linecap="round"/><line x1="1" y1="8" x2="21" y2="8" stroke="#1F6E8C" stroke-width="1.5" stroke-linecap="round"/><line x1="1" y1="15" x2="21" y2="15" stroke="#1F6E8C" stroke-width="1.5" stroke-linecap="round"/></svg></button>
                </div>

                <!-- BROWSE VIEW -->
                <div class="invitado-browse" id="invitadoBrowse">
                    <div class="invitado-search-wrap">
                        <div class="invitado-search-field">
                            <input type="text" class="invitado-search" id="invitadoSearch" placeholder="Search by a name, city, or any word." oninput="invitadoFilter()" autocomplete="off" autocorrect="off" spellcheck="false">
                            <button type="button" class="invitado-search-clear" id="invitadoSearchClear" onclick="invitadoSearchClear()" aria-label="Clear search" hidden>
                                <svg width="10" height="10" viewBox="0 0 10 10" fill="none" aria-hidden="true">
                                    <path d="M2.5 2.5 L7.5 7.5 M7.5 2.5 L2.5 7.5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>
                                </svg>
                            </button>
                        </div>
                    </div>
                    <button class="invitado-surprise" onclick="invitadoSurprise()" aria-label="Meet a random guest" title="Meet a random guest">
                        <svg viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg" fill="#1F6E8C" aria-hidden="true">
                            <!-- Flower head: 12 slim petals ringed around a tiny center.
                                 More petals + thinner shape keeps the silhouette reading
                                 airy and delicate at 32px. -->
                            <g transform="translate(16 12)">
                                <path d="M 0 -9 C -1.1 -8 -1.1 -2.5 0 -1.8 C 1.1 -2.5 1.1 -8 0 -9 Z"/>
                                <path d="M 0 -9 C -1.1 -8 -1.1 -2.5 0 -1.8 C 1.1 -2.5 1.1 -8 0 -9 Z" transform="rotate(30)"/>
                                <path d="M 0 -9 C -1.1 -8 -1.1 -2.5 0 -1.8 C 1.1 -2.5 1.1 -8 0 -9 Z" transform="rotate(60)"/>
                                <path d="M 0 -9 C -1.1 -8 -1.1 -2.5 0 -1.8 C 1.1 -2.5 1.1 -8 0 -9 Z" transform="rotate(90)"/>
                                <path d="M 0 -9 C -1.1 -8 -1.1 -2.5 0 -1.8 C 1.1 -2.5 1.1 -8 0 -9 Z" transform="rotate(120)"/>
                                <path d="M 0 -9 C -1.1 -8 -1.1 -2.5 0 -1.8 C 1.1 -2.5 1.1 -8 0 -9 Z" transform="rotate(150)"/>
                                <path d="M 0 -9 C -1.1 -8 -1.1 -2.5 0 -1.8 C 1.1 -2.5 1.1 -8 0 -9 Z" transform="rotate(180)"/>
                                <path d="M 0 -9 C -1.1 -8 -1.1 -2.5 0 -1.8 C 1.1 -2.5 1.1 -8 0 -9 Z" transform="rotate(210)"/>
                                <path d="M 0 -9 C -1.1 -8 -1.1 -2.5 0 -1.8 C 1.1 -2.5 1.1 -8 0 -9 Z" transform="rotate(240)"/>
                                <path d="M 0 -9 C -1.1 -8 -1.1 -2.5 0 -1.8 C 1.1 -2.5 1.1 -8 0 -9 Z" transform="rotate(270)"/>
                                <path d="M 0 -9 C -1.1 -8 -1.1 -2.5 0 -1.8 C 1.1 -2.5 1.1 -8 0 -9 Z" transform="rotate(300)"/>
                                <path d="M 0 -9 C -1.1 -8 -1.1 -2.5 0 -1.8 C 1.1 -2.5 1.1 -8 0 -9 Z" transform="rotate(330)"/>
                                <circle r="1.8"/>
                            </g>
                            <!-- Thin S-curved stem. -->
                            <path d="M 16 19 Q 18 22 14 25 Q 12 28 16 30" stroke="#1F6E8C" stroke-width="1.1" stroke-linecap="round" fill="none"/>
                            <!-- Two small pointed leaves, one each side of the stem. -->
                            <path d="M 14.5 23 C 10 21.5 8.5 24 10.5 25 C 12.5 24.8 14 24 14.5 23 Z"/>
                            <path d="M 16.5 27 C 21 25.5 22.5 28 20.5 29 C 18.5 28.8 17 28 16.5 27 Z"/>
                        </svg>
                    </button>
                    <div class="invitado-grid" id="invitadoGrid"></div>
                    <div class="invitado-empty" id="invitadoEmpty" style="display:none;">No one matches.</div>
                </div>

                <!-- PROFILE VIEW -->
                <div class="invitado-profile" id="invitadoProfile" style="display:none;">
                    <div class="invitado-card" id="invitadoCard"></div>
                    <div class="invitado-nav">
                        <button class="invitado-arrow" onclick="invitadoPrev()" aria-label="Previous guest">
                            <svg width="28" height="28" viewBox="0 0 40 40" fill="none">
                                <path d="M28 10 C24 16, 14 17, 8 20 C14 23, 24 24, 28 30" stroke="#1F6E8C" stroke-width="2.5" fill="none" stroke-linecap="round"/>
                            </svg>
                        </button>
                        <button class="invitado-arrow" onclick="invitadoNext()" aria-label="Next guest">
                            <svg width="28" height="28" viewBox="0 0 40 40" fill="none">
                                <path d="M12 10 C16 16, 26 17, 32 20 C26 23, 16 24, 12 30" stroke="#1F6E8C" stroke-width="2.5" fill="none" stroke-linecap="round"/>
                            </svg>
                        </button>
                    </div>
                </div>
            </div>

            <!-- TOASTS SCREEN -->
            <div class="screen" id="toasts">
                <div class="header">
                    <h1 class="header-title">Toasts</h1>
                    <button class="hamburger" onclick="toggleMenu()"><svg width="22" height="16" viewBox="0 0 22 16" fill="none"><line x1="1" y1="1" x2="21" y2="1" stroke="#1F6E8C" stroke-width="1.5" stroke-linecap="round"/><line x1="1" y1="8" x2="21" y2="8" stroke="#1F6E8C" stroke-width="1.5" stroke-linecap="round"/><line x1="1" y1="15" x2="21" y2="15" stroke="#1F6E8C" stroke-width="1.5" stroke-linecap="round"/></svg></button>
                </div>
                <div class="screen-content">
{{TOASTS}}
                </div>
            </div>

            <!-- SAN MIGUEL GUIDE SCREEN -->
            <div class="screen" id="guide">
                <div class="header">
                    <h1 class="header-title">Local Guide</h1>
                    <button class="hamburger" onclick="toggleMenu()"><svg width="22" height="16" viewBox="0 0 22 16" fill="none"><line x1="1" y1="1" x2="21" y2="1" stroke="#1F6E8C" stroke-width="1.5" stroke-linecap="round"/><line x1="1" y1="8" x2="21" y2="8" stroke="#1F6E8C" stroke-width="1.5" stroke-linecap="round"/><line x1="1" y1="15" x2="21" y2="15" stroke="#1F6E8C" stroke-width="1.5" stroke-linecap="round"/></svg></button>
                </div>
                <div class="screen-content">
                    <div class="guide-intro">Hilary has been coming to Wiscasset since 2013 when her parents and uncle bought a house here. This area is known as &ldquo;the prettiest village in Maine,&rdquo; and lives up to it by being quaint as hell. With a small, walkable downtown and some hidden (and some <a href="https://newengland.com/travel/maine/reds-eats-lobster-roll/" target="_blank" style="color: var(--primary-green);">not-so-hidden</a>) gems, we encourage you to explore the area we&rsquo;ve fallen in love with!</div>
{{GUIDE}}
                </div>
            </div>

            <!-- FOR A GOOD TIME SCREEN (hamburger menu) -->
            <div class="screen" id="goodtime">
                <div class="header">
                    <span></span>
                    <button class="hamburger" onclick="toggleMenu()"><svg width="22" height="16" viewBox="0 0 22 16" fill="none"><line x1="1" y1="1" x2="21" y2="1" stroke="#1F6E8C" stroke-width="1.5" stroke-linecap="round"/><line x1="1" y1="8" x2="21" y2="8" stroke="#1F6E8C" stroke-width="1.5" stroke-linecap="round"/><line x1="1" y1="15" x2="21" y2="15" stroke="#1F6E8C" stroke-width="1.5" stroke-linecap="round"/></svg></button>
                </div>
                <div class="screen-content">
                    <div class="goodtime-content">
                        <div class="goodtime-heading">Wiscasset Is<br>Best With:</div>

                        <div class="goodtime-tip">
                            <span class="goodtime-tip-icon" aria-hidden="true">
                                <!-- Jacket / layer silhouette -->
                                <svg viewBox="0 0 48 32"><path d="M19 5 L14 8 L10 13 L13 16 L16 14 L16 27 L32 27 L32 14 L35 16 L38 13 L34 8 L29 5 L24 9 Z" stroke-width="1.5"/></svg>
                            </span>
                            <div class="goodtime-tip-name">Clothing</div>
                            <div class="goodtime-tip-body">Bring layers as the temperature drops at night.</div>
                        </div>

                        <div class="goodtime-tip">
                            <span class="goodtime-tip-icon" aria-hidden="true">
                                <!-- Hand-illustrated sandal from the Noun Project. Tinted to primary green via CSS filter. -->
                                <img class="goodtime-tip-img" src="images/icons/sandal.png" alt="" width="48" height="48">
                            </span>
                            <div class="goodtime-tip-name">Shoes</div>
                            <div class="goodtime-tip-body">Prepare for lots of dancing at the venue and after party. The ceremony also takes place on grass.</div>
                        </div>

                        <div class="goodtime-tip">
                            <span class="goodtime-tip-icon" aria-hidden="true">
                                <!-- Paw print -->
                                <svg viewBox="0 0 48 32"><ellipse cx="24" cy="23" rx="10" ry="7" stroke-width="1.5"/><circle cx="10" cy="12" r="3.2" stroke-width="1.5"/><circle cx="19" cy="6" r="3.2" stroke-width="1.5"/><circle cx="29" cy="6" r="3.2" stroke-width="1.5"/><circle cx="38" cy="12" r="3.2" stroke-width="1.5"/></svg>
                            </span>
                            <div class="goodtime-tip-name">Georgie</div>
                            <div class="goodtime-tip-body">Sadly, Georgie will not be in attendance. Sorry to disappoint.</div>
                        </div>
                    </div>
                </div>
            </div>

            <!-- FAQ SCREEN (HAMBURGER) -->
            <div class="screen" id="faq">
                <div class="header">
                    <h1 class="header-title">FAQs</h1>
                    <button class="hamburger" onclick="toggleMenu()"><svg width="22" height="16" viewBox="0 0 22 16" fill="none"><line x1="1" y1="1" x2="21" y2="1" stroke="#1F6E8C" stroke-width="1.5" stroke-linecap="round"/><line x1="1" y1="8" x2="21" y2="8" stroke="#1F6E8C" stroke-width="1.5" stroke-linecap="round"/><line x1="1" y1="15" x2="21" y2="15" stroke="#1F6E8C" stroke-width="1.5" stroke-linecap="round"/></svg></button>
                </div>
                <div class="screen-content">
{{FAQS}}
                </div>
            </div>

            <!-- PHOTO ALBUM SCREEN -->
            <div class="screen" id="album">
                <div class="header">
                    <h1 class="header-title">Photo Album</h1>
                    <button class="hamburger" onclick="toggleMenu()"><svg width="22" height="16" viewBox="0 0 22 16" fill="none"><line x1="1" y1="1" x2="21" y2="1" stroke="#1F6E8C" stroke-width="1.5" stroke-linecap="round"/><line x1="1" y1="8" x2="21" y2="8" stroke="#1F6E8C" stroke-width="1.5" stroke-linecap="round"/><line x1="1" y1="15" x2="21" y2="15" stroke="#1F6E8C" stroke-width="1.5" stroke-linecap="round"/></svg></button>
                </div>
                <div class="screen-content">
                    <div class="album-card">
                        <div class="album-title">Our Shared Photo Album</div>
                        <div class="album-note">We'd love for you to add your photos from the weekend! Every moment matters—from getting ready to dancing until dawn.</div>
                        <a class="btn btn-primary" href="https://photos.app.goo.gl/Wd9K9bGdymA4u4mb8" target="_blank" rel="noopener" style="display: inline-block; text-decoration: none;">Open Album</a>
                    </div>
                </div>
            </div>

            <!-- COFFEE MAP SCREEN -->
            <div class="screen" id="coffee">
                <div class="header">
                    <h1 class="header-title">Coffee Map</h1>
                    <button class="hamburger" onclick="toggleMenu()"><svg width="22" height="16" viewBox="0 0 22 16" fill="none"><line x1="1" y1="1" x2="21" y2="1" stroke="#1F6E8C" stroke-width="1.5" stroke-linecap="round"/><line x1="1" y1="8" x2="21" y2="8" stroke="#1F6E8C" stroke-width="1.5" stroke-linecap="round"/><line x1="1" y1="15" x2="21" y2="15" stroke="#1F6E8C" stroke-width="1.5" stroke-linecap="round"/></svg></button>
                </div>
                <div class="screen-content">
                    <div class="coffee-map-placeholder">[Illustrated Coffee Map — v2]</div>
                </div>
            </div>

            <!-- REGISTRY SCREEN -->
            <div class="screen" id="registry">
                <div class="header">
                    <span></span>
                    <button class="hamburger" onclick="toggleMenu()"><svg width="22" height="16" viewBox="0 0 22 16" fill="none"><line x1="1" y1="1" x2="21" y2="1" stroke="#1F6E8C" stroke-width="1.5" stroke-linecap="round"/><line x1="1" y1="8" x2="21" y2="8" stroke="#1F6E8C" stroke-width="1.5" stroke-linecap="round"/><line x1="1" y1="15" x2="21" y2="15" stroke="#1F6E8C" stroke-width="1.5" stroke-linecap="round"/></svg></button>
                </div>
                <div class="screen-content">
                    <div style="text-align: center; margin-bottom: 24px;">
                        <div style="font-family: 'Mea Culpa', cursive; font-size: 32px; color: var(--primary-green); margin-bottom: 8px;">Gifts</div>
                        <div style="font-family: 'Bodoni Moda', serif; font-size: 14px; color: var(--dark); line-height: 1.7;">Your presence is literally a present, but if you want to buy a gift you can access our registry on Zola <a href="https://www.zola.com/registry/hilaryandelliott" target="_blank" style="color: var(--primary-green);">here</a>!</div>
                    </div>

                    <div style="margin-bottom: 24px;">
                        <div style="font-family: 'Mea Culpa', cursive; font-size: 20px; color: var(--primary-green); margin-bottom: 12px;">Causes Close to Our Hearts</div>

                        <div style="background: white; border: 1px solid var(--divider); border-radius: 12px; padding: 16px; margin-bottom: 12px;">
                            <div style="font-family: 'Bodoni Moda', serif; font-size: 18px; font-weight: 600; color: var(--dark); margin-bottom: 6px;">Pretrial Bail Freedom Fund</div>
                            <div style="font-family: 'Nunito', sans-serif; font-size: 16px; color: var(--dark); line-height: 1.6; margin-bottom: 10px;">Responds to the daily violence of the criminal legal system by paying bail and bringing people home from pre-trial detention.</div>
                            <a href="https://secure.actblue.com/donate/pretrialfreedom" target="_blank" style="font-family: 'Nunito', sans-serif; font-size: 16px; color: var(--primary-green); word-break: break-all;">secure.actblue.com/donate/pretrialfreedom</a>
                        </div>

                        <div style="background: white; border: 1px solid var(--divider); border-radius: 12px; padding: 16px; margin-bottom: 12px;">
                            <div style="font-family: 'Bodoni Moda', serif; font-size: 18px; font-weight: 600; color: var(--dark); margin-bottom: 6px;">Immigration Bond Freedom Fund</div>
                            <div style="font-family: 'Nunito', sans-serif; font-size: 16px; color: var(--dark); line-height: 1.6; margin-bottom: 10px;">A system of community-led immigration bond funds that raise money to free our friends and neighbors from immigration detention, intervene in deportations, and ensure people can pursue their cases from a place of freedom &amp; keep their families and communities together.</div>
                            <a href="https://secure.actblue.com/donate/immbondfreedom" target="_blank" style="font-family: 'Nunito', sans-serif; font-size: 16px; color: var(--primary-green); word-break: break-all;">secure.actblue.com/donate/immbondfreedom</a>
                        </div>
                    </div>
                </div>
            </div>

            <!-- TRAVEL INFO SCREEN -->
            <div class="screen" id="travel">
                <div class="header">
                    <h1 class="header-title" style="font-size: 16px; font-weight: 400; color: #666666; letter-spacing: 0.5px;">Travel Information</h1>
                    <button class="hamburger" onclick="toggleMenu()"><svg width="22" height="16" viewBox="0 0 22 16" fill="none"><line x1="1" y1="1" x2="21" y2="1" stroke="#1F6E8C" stroke-width="1.5" stroke-linecap="round"/><line x1="1" y1="8" x2="21" y2="8" stroke="#1F6E8C" stroke-width="1.5" stroke-linecap="round"/><line x1="1" y1="15" x2="21" y2="15" stroke="#1F6E8C" stroke-width="1.5" stroke-linecap="round"/></svg></button>
                </div>
                <div class="screen-content">

                    <!-- OUR RECOMMENDATION -->
                    <div style="margin-bottom: 20px;">
                        <div style="font-family: 'Mea Culpa', cursive; font-size: 20px; color: var(--primary-green); margin-bottom: 8px;">A quick note</div>
                        <div style="font-family: 'Bodoni Moda', serif; font-size: 14px; color: var(--dark); line-height: 1.6; font-style: italic;">This area has a lot of great food, including lobster/seafood shacks (Red&rsquo;s Eats, Sprague&rsquo;s), ice cream (Blanchard&rsquo;s Creamery, Round Top Ice Cream, Sweetz &amp; More), and more sit-down dinner spots (Water Street Kitchen, Montsweag Roadhouse). There are great places for coffee &amp; pastries (Wild Oats Bakery &amp; Cafe, Treats), and oysters straight from the source (Glidden Point Oyster Farm, Eros Oyster Farm). If you&rsquo;re looking to buy a little gift, stop by Westport Island Pottery (a quaint little shack of homemade pottery) or Rock Paper Scissors downtown. For a dip into nature, check out the Coastal Maine Botanical Gardens, Fort Edgecomb, or Chewonki&rsquo;s Cushman Preserve.</div>
                    </div>

                    <!-- CAR SERVICES -->
                    <div style="margin-bottom: 24px;">
                        <div style="font-family: 'Mea Culpa', cursive; font-size: 20px; color: var(--primary-green); margin-bottom: 12px;">Car Services, Shuttles &amp; Rental Cars</div>
                        <div style="font-family: 'Bodoni Moda', serif; font-size: 14px; color: var(--dark); line-height: 1.6; margin-bottom: 16px;">Rental cars are the best way to get around coastal Maine, though there are also shuttles and buses from Portland if you&rsquo;d prefer not to rent a car. On the wedding day we will have shuttles to take guests to and from the wedding, and taxis to take people home from the after party. Uber/Lyft may be possible, but is not reliable in this part of Maine.<br><br>As many people will be coming from Boston, Portland, and New York, we are creating a carpool spreadsheet for folks to fill out in case there are extra seats to be shared. If you are driving and open to taking more passengers, please fill this out!</div>

{{CAR_SERVICES}}
                    </div>

                    <!-- TRAVEL COORDINATION -->
                    <div style="background: white; border: 1px solid var(--divider); border-radius: 12px; padding: 16px; margin-bottom: 24px; text-align: center;">
                        <div style="font-family: 'Mea Culpa', cursive; font-size: 20px; color: var(--primary-green); margin-bottom: 8px;">Share Your Travel Info</div>
                        <div style="font-family: 'Bodoni Moda', serif; font-size: 14px; color: var(--dark); line-height: 1.6; margin-bottom: 12px;">Help us know when you'll be in town by dropping your travel information here! (And in case people want to share car services to and from the airports as well.)</div>
                        <a href="https://docs.google.com/spreadsheets/d/14hR2oTxASCtvXdpxfFavTYS_p_7wOgDBfxv_paEYN9Q/edit?gid=0#gid=0" target="_blank" class="btn btn-primary" style="display: inline-block; text-decoration: none;">Add Arrival &amp; Departure Info</a>
                    </div>

                    <!-- FLIGHTS -->
                    <div style="margin-bottom: 24px;">
                        <div style="font-family: 'Mea Culpa', cursive; font-size: 20px; color: var(--primary-green); margin-bottom: 12px;">Airports &amp; Flights</div>

{{AIRPORTS}}
                    </div>

                    <!-- HOUSES & HOTELS -->
                    <div style="margin-bottom: 24px;">
                        <div style="font-family: 'Mea Culpa', cursive; font-size: 20px; color: var(--primary-green); margin-bottom: 8px;">Houses &amp; Hotels</div>
                        <div style="font-family: 'Bodoni Moda', serif; font-size: 14px; color: var(--dark); line-height: 1.6; margin-bottom: 12px;">We recommend <a href="https://maps.app.goo.gl/ArArf5KeFNLubTV19" target="_blank" style="color: var(--primary-green);">Wiscasset Woods Lodge</a> (code: LudlowEngelman for 10% off!) or the <a href="https://maps.app.goo.gl/Eu4mr3eE5R1stZVF7" target="_blank" style="color: var(--primary-green);">Cod Cove Inn</a>. There are also a few Airbnb rooms available so reach out to Hilary or Elliott if you&rsquo;re still in need of lodging!</div>
                    </div>
                </div>
            </div>

            <!-- HAMBURGER MENU -->
            <div class="menu-overlay" id="menuOverlay" onclick="closeMenu()"></div>
            <div class="menu-drawer" id="menuDrawer">
                <!-- Items are alphabetized. The four bottom-tab destinations
                     (Schedule, Who's Coming, San Miguel Guide, Travel/Toasts)
                     are mirrored here so they're reachable from the menu too;
                     they call switchScreen(id, tabIndex) so the active tab
                     indicator updates. The Toasts/Travel slot is date-gated:
                     before 2026-09-19 the Travel item is visible (Toasts hidden),
                     on/after the Toasts item is visible (Travel hidden). Toggled
                     by initHamburgerDateSwap(). -->
                <button class="menu-item install-menu-item" id="installMenuItem" onclick="installApp()">Download the App</button>
                <a class="menu-item" href="https://chat.whatsapp.com/LYpQT10pMmt6Bm3rKztsu1?mode=gi_t" target="_blank" rel="noopener noreferrer" onclick="closeMenu()" style="text-decoration:none;color:inherit;display:block;">Chat</a>
                <button class="menu-item" onclick="switchScreenFromMenu('extras')">Extra Activities</button>
                <button class="menu-item menu-item--good-time" onclick="switchScreenFromMenu('goodtime')"><span>To Enjoy Your Time</span><span class="menu-item-icon" aria-hidden="true"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"><circle cx="8" cy="8" r="2.6"/><line x1="8" y1="2" x2="8" y2="3.4"/><line x1="8" y1="12.6" x2="8" y2="14"/><line x1="2" y1="8" x2="3.4" y2="8"/><line x1="12.6" y1="8" x2="14" y2="8"/><line x1="3.76" y1="3.76" x2="4.75" y2="4.75"/><line x1="11.25" y1="11.25" x2="12.24" y2="12.24"/><line x1="3.76" y1="12.24" x2="4.75" y2="11.25"/><line x1="11.25" y1="4.75" x2="12.24" y2="3.76"/></svg></span></button>
                <button class="menu-item" onclick="switchScreen('facebook', 0)">Who's Coming</button>
                <button class="menu-item" onclick="switchScreenFromMenu('registry')">Registry</button>
                <button class="menu-item" onclick="switchScreen('guide', 2)">Local Guide</button>
                <button class="menu-item" onclick="switchScreenFromMenu('schedule')">Schedule</button>
                <button class="menu-item" id="hamburger-toasts" onclick="switchScreen('toasts', 1)">Toasts</button>
                <button class="menu-item" id="hamburger-travel" onclick="switchScreen('travel', 1)">Travel</button>
                <div class="menu-version" aria-hidden="true">v{{VERSION}}</div>
            </div>

            <!-- INSTALL CTA + INSTRUCTIONS (Add to Home Screen flow) -->
            <button class="install-cta" id="installCta" type="button" onclick="installApp()" aria-label="Download the app">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                    <path d="M12 3v12" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
                    <path d="M7 10l5 5 5-5" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
                    <path d="M5 20h14" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
                </svg>
                <span>Download the App</span>
            </button>

            <div class="install-overlay" id="installInstructions" onclick="if (event.target === this) hideInstallInstructions()">
                <div class="install-sheet" role="dialog" aria-labelledby="installSheetTitle">
                    <h2 id="installSheetTitle">Download the App</h2>
                    <div id="installSheetIos">
                        <p>Open this page in Safari, then:</p>
                        <ol>
                            <li>Tap the Share button
                                <svg class="share-glyph" width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                                    <path d="M12 3v13" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
                                    <path d="M8 7l4-4 4 4" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
                                    <path d="M5 12v7a2 2 0 002 2h10a2 2 0 002-2v-7" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
                                </svg>
                                at the bottom of the screen.
                            </li>
                            <li>Scroll and choose <strong>Add to Home Screen</strong>.</li>
                            <li>Tap <strong>Add</strong> in the top right.</li>
                        </ol>
                    </div>
                    <div id="installSheetAndroid" style="display:none;">
                        <p>To add this app to your home screen:</p>
                        <ol>
                            <li>Open your browser menu (the <strong>⋮</strong> in the corner).</li>
                            <li>Tap <strong>Install app</strong> or <strong>Add to Home Screen</strong>.</li>
                            <li>Confirm to add the icon.</li>
                        </ol>
                    </div>
                    <div id="installSheetGeneric" style="display:none;">
                        <p>Open this page in your phone's browser (Safari on iPhone, Chrome on Android), then use the browser menu to add it to your home screen.</p>
                    </div>
                    <button class="install-sheet-close" type="button" onclick="hideInstallInstructions()">Got it</button>
                </div>
            </div>

            <!-- TAB BAR
                 4 tabs: Who's Coming (the app's home), a date-gated
                 Travel/Toasts slot (Travel before 2026-09-19, Toasts
                 on/after — see initTravelToastsTab()), San Miguel Guide,
                 and Share Photos (an external link to the shared Google
                 Photos album, opens in a new tab — never gets the
                 .active highlight). Schedule is no longer in the tab
                 bar; it's accessible via the hamburger menu. -->
            <div class="tab-bar">
                <button class="tab active" onclick="switchScreen('facebook', 0)">
                    <div class="tab-icon">
                        <!-- Three people grouped: side figures slightly smaller
                             and behind the slightly-taller center figure. -->
                        <svg viewBox="0 0 28 28" fill="none">
                            <circle cx="7" cy="11" r="2.5" stroke-width="1.8" fill="none"/>
                            <circle cx="14" cy="8" r="3" stroke-width="1.8" fill="none"/>
                            <circle cx="21" cy="11" r="2.5" stroke-width="1.8" fill="none"/>
                            <path d="M1 22 C1 18, 3 15, 7 15 C9 15, 10 15.5, 10.5 16" stroke-width="1.8" stroke-linecap="round" fill="none"/>
                            <path d="M8 24 C8 19, 10 14, 14 14 C18 14, 20 19, 20 24" stroke-width="1.8" stroke-linecap="round" fill="none"/>
                            <path d="M27 22 C27 18, 25 15, 21 15 C19 15, 18 15.5, 17.5 16" stroke-width="1.8" stroke-linecap="round" fill="none"/>
                        </svg>
                    </div>
                    <span class="tab-label">Who's Coming</span>
                </button>
                <button class="tab" id="travelToastsTab" onclick="onTravelToastsTap()">
                    <div class="tab-icon" id="travelToastsIcon">
                        <!-- Default (rendered on first paint before JS runs):
                             hand-drawn top-down airplane silhouette per the
                             design-system v2 spec. Swept wings, twin tailplanes,
                             pointed nose. Line art only, stroke overridden by
                             the .tab-icon CSS. -->
                        <svg viewBox="0 0 28 28" fill="none">
                            <path d="M14 2.5 L15 3 L15 10.5 L25 15 L25 16.5 L15 14.5 L15 20 L18.5 22 L18.5 23.5 L14.5 22.5 L14 23 L13.5 22.5 L9.5 23.5 L9.5 22 L13 20 L13 14.5 L3 16.5 L3 15 L13 10.5 L13 3 Z" stroke-width="1.8" fill="none" stroke-linejoin="round" stroke-linecap="round"/>
                        </svg>
                    </div>
                    <span class="tab-label" id="travelToastsLabel">Travel</span>
                </button>
                <button class="tab" onclick="switchScreen('guide', 2)">
                    <div class="tab-icon">
                        <!-- La Parroquia silhouette: three narrow gothic spires
                             rising from a shared base, with a tall central spire
                             topped by a small cross. San Miguel's signature skyline.
                             (Kept over design-system v2's "folded map" at Elien's
                             request — v2 icon swap skipped.) -->
                        <svg viewBox="0 0 28 28" fill="none">
                            <polyline points="4,24 4,15 7,11 10,15 10,24" stroke-width="1.8" fill="none" stroke-linejoin="round" stroke-linecap="round"/>
                            <polyline points="11,24 11,10 14,4 17,10 17,24" stroke-width="1.8" fill="none" stroke-linejoin="round" stroke-linecap="round"/>
                            <polyline points="18,24 18,15 21,11 24,15 24,24" stroke-width="1.8" fill="none" stroke-linejoin="round" stroke-linecap="round"/>
                            <line x1="14" y1="1" x2="14" y2="4" stroke-width="1.8" stroke-linecap="round"/>
                            <line x1="12.5" y1="2.5" x2="15.5" y2="2.5" stroke-width="1.8" stroke-linecap="round"/>
                            <line x1="2" y1="24" x2="26" y2="24" stroke-width="1.8" stroke-linecap="round"/>
                        </svg>
                    </div>
                    <span class="tab-label">Guide</span>
                </button>
                <a class="tab" href="https://photos.app.goo.gl/Wd9K9bGdymA4u4mb8" target="_blank" rel="noopener noreferrer" aria-label="Share photos to the shared album">
                    <div class="tab-icon">
                        <!-- Camera silhouette in the same line-art style as
                             the other tab icons: rounded body, viewfinder
                             hump on top, lens circle, and a tiny solid
                             flash dot. -->
                        <svg viewBox="0 0 28 28" fill="none">
                            <rect x="3" y="9" width="22" height="14" rx="2.5" stroke-width="1.8" fill="none"/>
                            <path d="M10 9 L11.5 6 L16.5 6 L18 9" stroke-width="1.8" fill="none" stroke-linejoin="round" stroke-linecap="round"/>
                            <circle cx="14" cy="16" r="4" stroke-width="1.8" fill="none"/>
                            <circle cx="20.5" cy="12" r="0.7" class="dot"/>
                        </svg>
                    </div>
                    <span class="tab-label">Photos</span>
                </a>
            </div>

    </div>

    <script>
        // Guest data
        const guests = {{GUESTS_JSON}};
        // Linked-partner stubs (key → minimal profile). These are
        // attending guests who haven't filled the form but are tagged
        // as someone's romantic partner. They never appear in the browse
        // grid or the swipe sequence — only chip clicks land here.
        const partnerProfilesArr = {{PARTNER_PROFILES_JSON}};
        const partnerProfilesByKey = Object.fromEntries(partnerProfilesArr.map(p => [p.key, p]));

        const avatarColors = ['avatar-1', 'avatar-2', 'avatar-3', 'avatar-4', 'avatar-5'];
        let currentScreen = 'facebook';
        let hasSeenSplash = false;
        let userName = '';
        let currentGuest = null;

        // Hash-based deep-link router.
        // Routes: #/schedule, #/invitados, #/invitados/<guest-key>, #/toasts,
        // #/guide, #/coffee, #/travel, #/registry, #/album, #/faq, #/goodtime,
        // #/extras. login and splash are intentionally unroutable.
        const SCREEN_HASHES = {
            schedule: 'schedule',
            facebook: 'invitados',
            toasts: 'toasts',
            guide: 'guide',
            coffee: 'coffee',
            travel: 'travel',
            registry: 'registry',
            album: 'album',
            faq: 'faq',
            goodtime: 'goodtime',
            extras: 'extras',
        };
        const HASH_TO_SCREEN = Object.fromEntries(
            Object.entries(SCREEN_HASHES).map(([k, v]) => [v, k])
        );
        // Tab indexes for the bottom bar. Travel and Toasts both occupy
        // slot 2 — which one renders depends on the date-gated swap.
        // Tab indexes for the bottom bar after the design-system v3 reorder:
        // Who's Coming (0), Travel/Toasts (1, date-gated), San Miguel (2),
        // Photos (3, external link — never highlighted). Schedule no longer
        // has a tab; it routes via switchScreenFromMenu so no tab is marked
        // active when a hamburger user lands there.
        const TAB_INDEX = { facebook: 0, travel: 1, toasts: 1, guide: 2 };
        let isApplyingHash = false;

        // Guest lookup for fuzzy matching and RSVP filtering (from wedding.db)
        const guestLookup = {{GUEST_LOOKUP_JSON}};

        function levenshtein(a, b) {
            const m = a.length, n = b.length;
            const dp = Array.from({length: m + 1}, () => Array(n + 1).fill(0));
            for (let i = 0; i <= m; i++) dp[i][0] = i;
            for (let j = 0; j <= n; j++) dp[0][j] = j;
            for (let i = 1; i <= m; i++)
                for (let j = 1; j <= n; j++)
                    dp[i][j] = Math.min(
                        dp[i-1][j] + 1,
                        dp[i][j-1] + 1,
                        dp[i-1][j-1] + (a[i-1] !== b[j-1] ? 1 : 0)
                    );
            return dp[m][n];
        }

        function fuzzyMatchGuest(input) {
            const q = input.toLowerCase().trim();
            if (!q) return null;
            const real = guestLookup.filter(g => g.f.toLowerCase() !== 'guest');
            let best = null, bestScore = Infinity;
            for (const g of real) {
                const first = g.f.toLowerCase();
                const last = g.l.toLowerCase();
                const full = g.n.toLowerCase();
                if (first === q) return g;
                if (full === q) return g;
                if (last && q === first + last[0]) return g;
                const d1 = levenshtein(q, first);
                const d2 = levenshtein(q, full);
                const d3 = last ? levenshtein(q, first + last[0]) : Infinity;
                const score = Math.min(d1, d2, d3);
                if (score < bestScore) { bestScore = score; best = g; }
            }
            const maxDist = Math.max(2, Math.floor(q.length * 0.4));
            return bestScore <= maxDist ? best : null;
        }

        function applyGuestSchedule(guest) {
            // RSVPs are not used in this app; this only remembers who is
            // logged in. Every guest sees the full schedule.
            currentGuest = guest;
        }

        function handleLogin() {
            const nameInput = document.getElementById('nameInput').value.trim();
            if (!nameInput) return;
            const matched = fuzzyMatchGuest(nameInput);
            if (matched) {
                localStorage.setItem('guestId', matched.i);
                userName = matched.f.split(' ')[0];
                userName = userName.charAt(0).toUpperCase() + userName.slice(1).toLowerCase();
                applyGuestSchedule(matched);
            } else {
                const firstName = nameInput.split(/\s+/)[0];
                userName = firstName.charAt(0).toUpperCase() + firstName.slice(1).toLowerCase();
            }
            // Persist the splash state so a SW-driven reload (which fires
            // when an update activates while the user sits on the splash)
            // doesn't silently skip them past it via autoLogin().
            localStorage.setItem('splashPending', '1');
            switchScreen('splash', null);
            hasSeenSplash = true;
        }

        function skipSplash() {
            localStorage.removeItem('splashPending');
            // If the user landed on a deep link, route there instead of the home.
            const hash = (window.location.hash || '').replace(/^#\/?/, '');
            const head = hash.split('/').filter(Boolean)[0];
            if (head && HASH_TO_SCREEN[head]) {
                applyHash();
                return;
            }
            switchScreen('facebook', 0);
        }

        function updateHash(path) {
            if (isApplyingHash) return;
            const target = path ? '#' + path : '';
            if (window.location.hash === target) return;
            const url = target || window.location.pathname + window.location.search;
            history.pushState(null, '', url);
        }

        function hashForScreen(screenId) {
            const slug = SCREEN_HASHES[screenId];
            return slug ? '/' + slug : null;
        }

        function applyHash() {
            const loginActive = document.getElementById('login').classList.contains('active');
            if (loginActive) return;
            const raw = (window.location.hash || '').replace(/^#\/?/, '');
            if (!raw) return;
            const parts = raw.split('/').filter(Boolean);
            const [head, sub] = parts;
            const screenId = HASH_TO_SCREEN[head];
            if (!screenId) return;
            isApplyingHash = true;
            try {
                if (screenId in TAB_INDEX) {
                    switchScreen(screenId, TAB_INDEX[screenId]);
                } else {
                    switchScreenFromMenu(screenId);
                }
                if (screenId === 'facebook') {
                    if (sub && sub in invitadoKeyToIndex) {
                        invitadoOpenProfile(invitadoKeyToIndex[sub]);
                    } else if (sub && sub in partnerProfilesByKey) {
                        invitadoOpenPartnerProfile(sub);
                    } else {
                        invitadoShowBrowse();
                    }
                }
            } finally {
                isApplyingHash = false;
            }
        }

        // Auto-login if guest already identified
        (function autoLogin() {
            const savedId = localStorage.getItem('guestId');
            if (savedId) {
                const guest = guestLookup.find(g => g.i === parseInt(savedId, 10));
                if (guest) {
                    userName = guest.f.split(' ')[0];
                    userName = userName.charAt(0).toUpperCase() + userName.slice(1).toLowerCase();
                    applyGuestSchedule(guest);
                    hasSeenSplash = true;
                    document.getElementById('login').classList.remove('active');
                    // If the user was mid-splash when the page reloaded
                    // (e.g. SW update), keep them on the splash so they get
                    // to dismiss it themselves.
                    if (localStorage.getItem('splashPending')) {
                        document.getElementById('splash').classList.add('active');
                        document.querySelector('.tab-bar').style.display = 'none';
                        currentScreen = 'splash';
                    } else {
                        // Default home is Who's Coming (the browse grid).
                        // Mark the screen + tab active synchronously so
                        // the first paint is correct. The grid-render
                        // hook (invitadoEnsureGridRendered) and the
                        // browse-vs-profile state setter
                        // (invitadoShowBrowse) both touch `let`s declared
                        // further down in this script, so they're in TDZ
                        // during this IIFE — queueMicrotask defers the
                        // call until after the script finishes loading.
                        document.getElementById('facebook').classList.add('active');
                        document.querySelector('.tab-bar').style.display = 'flex';
                        document.querySelectorAll('.tab').forEach((t, i) => {
                            t.classList.toggle('active', i === 0);
                        });
                        currentScreen = 'facebook';
                        queueMicrotask(() => {
                            if (typeof invitadoEnsureGridRendered === 'function') invitadoEnsureGridRendered();
                            if (typeof invitadoShowBrowse === 'function') invitadoShowBrowse();
                        });
                    }
                }
            }
        })();

        function switchScreen(screenId, tabIndex) {
            document.querySelectorAll('.screen').forEach(s => s.classList.remove('active'));
            document.getElementById(screenId).classList.add('active');
            currentScreen = screenId;

            // Tapping the Who's Coming tab icon always returns the user
            // to the browse grid, even if they're already on the tab with
            // a profile open. Otherwise the tap would feel like a no-op.
            if (screenId === 'facebook' && typeof invitadoShowBrowse === 'function') {
                // Render the grid the first time the screen actually becomes
                // visible. iOS Safari skips fetching <img> elements injected
                // into a display:none ancestor (even with loading="eager")
                // and they often never recover when the ancestor is later
                // shown — so we postpone the work until the user lands here.
                if (typeof invitadoEnsureGridRendered === 'function') invitadoEnsureGridRendered();
                invitadoShowBrowse();
            }

            const tabBar = document.querySelector('.tab-bar');
            if (screenId === 'login' || screenId === 'splash') {
                tabBar.style.display = 'none';
            } else {
                tabBar.style.display = 'flex';
            }

            if (tabIndex !== null) {
                document.querySelectorAll('.tab').forEach((t, i) => {
                    t.classList.toggle('active', i === tabIndex);
                });
            } else {
                document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            }

            closeMenu();
            const path = hashForScreen(screenId);
            if (path) updateHash(path);
            if (typeof updateInstallUi === 'function') updateInstallUi();
        }

        function switchScreenFromMenu(screenId) {
            document.querySelectorAll('.screen').forEach(s => s.classList.remove('active'));
            document.getElementById(screenId).classList.add('active');
            currentScreen = screenId;
            document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            closeMenu();
            const path = hashForScreen(screenId);
            if (path) updateHash(path);
            if (typeof updateInstallUi === 'function') updateInstallUi();
        }

        function toggleMenu() {
            document.getElementById('menuOverlay').classList.toggle('active');
            document.getElementById('menuDrawer').classList.toggle('active');
        }

        function closeMenu() {
            document.getElementById('menuOverlay').classList.remove('active');
            document.getElementById('menuDrawer').classList.remove('active');
        }

        function toggleEvent(element, e) {
            // Don't collapse the card when the user taps an internal link or button.
            if (e && e.target && e.target.closest && e.target.closest('a, button')) return;
            element.classList.toggle('expanded');
        }

        function toggleAccordion(element) {
            element.classList.toggle('open');
        }

        // Tap a toast-card header to expand its body (Persian text + audio).
        function toggleToast(element) {
            element.classList.toggle('open');
        }

        // Audio playback for a toast's <audio> element. The .audio-player
        // wrapper stops click-bubbling so taps on the controls don't also
        // collapse the surrounding toast-card. Only one toast plays at a
        // time — kicking off play() pauses any other currently-playing
        // toast first.
        function toggleToastAudio(btn, event) {
            if (event) event.stopPropagation();
            const player = btn.closest('.audio-player');
            const audio = player && player.querySelector('audio');
            if (!audio) return;
            if (audio.paused) {
                document.querySelectorAll('.audio-player audio').forEach((a) => {
                    if (a !== audio && !a.paused) a.pause();
                });
                const p = audio.play();
                if (p && typeof p.catch === 'function') p.catch(() => {});
            } else {
                audio.pause();
            }
        }

        // Click anywhere on the progress bar to seek. Maps the click X
        // within the bar to a fraction of the audio's known duration.
        // Bails out if duration isn't known yet (audio not loaded).
        function seekToastAudio(bar, event) {
            if (event) event.stopPropagation();
            const player = bar.closest('.audio-player');
            const audio = player && player.querySelector('audio');
            if (!audio || !isFinite(audio.duration) || audio.duration <= 0) return;
            const rect = bar.getBoundingClientRect();
            const ratio = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
            audio.currentTime = ratio * audio.duration;
        }

        // Wire each toast <audio> once: keep the play-btn icon, progress
        // fill, and duration text in sync with playback. The duration
        // text starts as the static "m:ss" rendered from the DB so the
        // card looks right before audio metadata loads; once the user
        // hits play we switch to "current / total". Re-running this is
        // a no-op (per-element data-toast-init guard).
        function initToastAudios() {
            const audios = document.querySelectorAll('.audio-player audio');
            audios.forEach((audio) => {
                if (audio.dataset.toastInit) return;
                audio.dataset.toastInit = '1';
                const player = audio.closest('.audio-player');
                const btn = player.querySelector('.play-btn');
                const fill = player.querySelector('.progress-fill');
                const durEl = player.querySelector('.duration');
                const baseDuration = durEl ? durEl.textContent : '';
                const fmt = (t) => {
                    if (!isFinite(t)) return baseDuration;
                    const m = Math.floor(t / 60);
                    const s = Math.floor(t % 60);
                    return m + ':' + (s < 10 ? '0' : '') + s;
                };
                audio.addEventListener('play', () => {
                    if (btn) {
                        btn.textContent = '⏸';
                        btn.setAttribute('aria-label', 'Pause');
                    }
                });
                audio.addEventListener('pause', () => {
                    if (btn) {
                        btn.textContent = '▶';
                        btn.setAttribute('aria-label', 'Play');
                    }
                });
                audio.addEventListener('ended', () => {
                    if (fill) fill.style.width = '0%';
                    if (durEl) durEl.textContent = baseDuration;
                    audio.currentTime = 0;
                });
                audio.addEventListener('timeupdate', () => {
                    if (fill && audio.duration) {
                        fill.style.width = (audio.currentTime / audio.duration * 100) + '%';
                    }
                    if (durEl && audio.duration) {
                        durEl.textContent = fmt(audio.currentTime) + ' / ' + fmt(audio.duration);
                    }
                });
            });
        }

        // Tap a place-card header to expand its body (description +
        // Get Directions). Mirrors the Travel page's event-card pattern.
        function togglePlaceCard(card) {
            card.classList.toggle('expanded');
        }

        // Tap a guide section's title row to collapse/expand its body.
        // Default state is expanded; collapsing lets readers skip past a
        // long section quickly using the jump-nav pills above.
        function toggleGuideSection(section) {
            if (!section) return;
            const collapsed = section.classList.toggle('collapsed');
            const btn = section.querySelector('.guide-section-toggle');
            if (btn) btn.setAttribute('aria-expanded', String(!collapsed));
        }

        // Jump-nav pills at the top of the San Miguel Guide. Smooth-scroll
        // the section into view inside its scrolling parent. If the target
        // is collapsed, expand it first so the reader actually sees content.
        //
        // We scroll .screen-content directly instead of using scrollIntoView
        // because both .screen and .screen-content are overflow-y:auto, and
        // scrollIntoView walks every scrollable ancestor — that scrolled
        // .screen too, which on iOS pushed the .header off the top of the
        // viewport and the bottom tab bar up with it.
        function scrollToGuideSection(id) {
            const el = document.getElementById(id);
            if (!el) return;
            if (el.classList.contains('collapsed')) {
                el.classList.remove('collapsed');
                const btn = el.querySelector('.guide-section-toggle');
                if (btn) btn.setAttribute('aria-expanded', 'true');
            }
            const scroller = el.closest('.screen-content');
            if (scroller) {
                const top = el.getBoundingClientRect().top
                          - scroller.getBoundingClientRect().top
                          + scroller.scrollTop;
                scroller.scrollTo({ top, behavior: 'smooth' });
            } else {
                el.scrollIntoView({ behavior: 'smooth', block: 'start' });
            }
        }

        // Legacy toggleCategory — kept for any old markup; new Guide
        // doesn't use category-section collapse but the function staying
        // around is safer than breaking referrers we may have missed.
        function toggleCategory(section) {
            section.classList.toggle('expanded');
        }

        // Date-gated Travel / Toasts tab.
        //   Before 2026-09-19 00:00 local time: the tab shows a plane icon
        //     labeled "Travel" and navigates to the #travel screen.
        //   On or after 2026-09-19 00:00: the tab shows the microphone icon
        //     labeled "Toasts" and navigates to the #toasts screen.
        // The switch honors the comment in microphone-icon.svg about the
        // Toasts feature only being relevant once guests are on site.
        const TOASTS_CUTOVER = new Date(2026, 8, 19, 0, 0, 0); // JS months are 0-indexed: 8 = September
        function isToastsTime() {
            return new Date() >= TOASTS_CUTOVER;
        }
        const TRAVEL_ICON_SVG = `
            <svg viewBox="0 0 28 28" fill="none">
                <path d="M14 2.5 L15 3 L15 10.5 L25 15 L25 16.5 L15 14.5 L15 20 L18.5 22 L18.5 23.5 L14.5 22.5 L14 23 L13.5 22.5 L9.5 23.5 L9.5 22 L13 20 L13 14.5 L3 16.5 L3 15 L13 10.5 L13 3 Z" stroke-width="1.8" fill="none" stroke-linejoin="round" stroke-linecap="round"/>
            </svg>`;
        const MIC_ICON_SVG = `
            <svg viewBox="0 0 28 28" fill="none">
                <g transform="rotate(20, 14, 13)">
                    <circle cx="14" cy="7" r="5" stroke-width="1.8" fill="none"/>
                    <line x1="11" y1="5" x2="17" y2="5" stroke-width="1.8" stroke-linecap="round"/>
                    <line x1="10" y1="7" x2="18" y2="7" stroke-width="1.8" stroke-linecap="round"/>
                    <line x1="10" y1="9" x2="18" y2="9" stroke-width="1.8" stroke-linecap="round"/>
                    <rect x="12" y="12" width="4" height="8" rx="1.5" stroke-width="1.8" fill="none"/>
                    <path d="M14 20 C14 22, 12 23, 9 24" stroke-width="1.8" stroke-linecap="round"/>
                </g>
            </svg>`;
        function initTravelToastsTab() {
            const iconEl = document.getElementById('travelToastsIcon');
            const labelEl = document.getElementById('travelToastsLabel');
            if (!iconEl || !labelEl) return;
            if (isToastsTime()) {
                iconEl.innerHTML = MIC_ICON_SVG;
                labelEl.textContent = 'Toasts';
            } else {
                iconEl.innerHTML = TRAVEL_ICON_SVG;
                labelEl.textContent = 'Travel';
            }
        }
        function onTravelToastsTap() {
            // Tab index 1 after the design-system v3 reorder
            // (Who's Coming 0, Travel/Toasts 1, San Miguel 2, Photos 3).
            if (isToastsTime()) {
                switchScreen('toasts', 1);
            } else {
                switchScreen('travel', 1);
            }
        }
        // Hamburger mirror of the date-swap: whichever of Toasts/Travel is
        // NOT in the tab bar stays accessible via the hamburger menu.
        //   Before 2026-09-19: tab bar has Travel → hamburger shows Toasts.
        //   On/after 2026-09-19: tab bar has Toasts → hamburger shows Travel.
        function initHamburgerDateSwap() {
            const travelItem = document.getElementById('hamburger-travel');
            const toastsItem = document.getElementById('hamburger-toasts');
            if (!travelItem || !toastsItem) return;
            if (isToastsTime()) {
                toastsItem.style.display = 'none';
                travelItem.style.display = '';
            } else {
                toastsItem.style.display = '';
                travelItem.style.display = 'none';
            }
        }

        // Run both on load so the correct icons/labels render before first paint.
        initTravelToastsTab();
        initHamburgerDateSwap();
        initToastAudios();

        // Who's Coming — browse grid + profile view
        let invitadoIndex = 0;
        const invitadoKeyToIndex = {};
        guests.forEach((g, i) => { invitadoKeyToIndex[g.key] = i; });

        function invitadoEscape(s) {
            const div = document.createElement('div');
            div.textContent = s == null ? '' : String(s);
            return div.innerHTML;
        }

        // ─── Stay in Touch helpers ─────────────────────────────────
        // Contact info gets stored canonically in wedding.db (bare
        // handles, https URLs, digits-only phone). These helpers build
        // the display label and the tappable href for each platform.

        // Inline SVG icons for the Stay in Touch list. Stroke-only,
        // 18×18, currentColor — colour comes from .invitado-contact-icon.
        const INVITADO_CONTACT_ICONS = {
            phone:     '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72 12.84 12.84 0 0 0 .7 2.81 2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45 12.84 12.84 0 0 0 2.81.7A2 2 0 0 1 22 16.92Z"/></svg>',
            email:     '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 7l9 6 9-6"/></svg>',
            instagram: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="5"/><circle cx="12" cy="12" r="4"/><circle cx="17.5" cy="6.5" r="1" fill="currentColor" stroke="none"/></svg>',
            linkedin:  '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M19 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V5a2 2 0 0 0-2-2zM8.339 18.337H5.667v-8.59h2.672v8.59zM7.003 8.574a1.548 1.548 0 1 1 0-3.096 1.548 1.548 0 0 1 0 3.096zm11.335 9.763h-2.669V14.16c0-.996-.018-2.277-1.388-2.277-1.39 0-1.601 1.086-1.601 2.207v4.248h-2.667v-8.59h2.56v1.174h.037a2.804 2.804 0 0 1 2.524-1.387c2.7 0 3.2 1.778 3.2 4.092v4.71z"/></svg>',
            twitter:   '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231zm-1.161 17.52h1.833L7.084 4.126H5.117z"/></svg>',
            bluesky:   '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M6.335 5.144C8.844 7.028 11.541 10.85 12.531 12.9c.99-2.05 3.687-5.872 6.196-7.756 1.81-1.358 4.741-2.408 4.741.937 0 .667-.382 5.604-.606 6.408-.781 2.792-3.629 3.504-6.16 3.073 4.426.754 5.554 3.25 3.122 5.745-4.62 4.736-6.638-1.19-7.155-2.706-.095-.279-.139-.408-.139-.298 0-.11-.044.019-.139.298-.518 1.517-2.535 7.442-7.155 2.706-2.432-2.495-1.304-4.99 3.122-5.745-2.531.43-5.379-.281-6.16-3.073C1.974 11.685 1.593 6.749 1.593 6.08c0-3.343 2.931-2.294 4.741-.937z"/></svg>',
            soundcloud:'<svg viewBox="0 0 24 24" fill="currentColor"><path d="M7 17V8a5 5 0 0 1 9.9-1 4 4 0 0 1 .1 8H7z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><path d="M3 14v3M5 12v5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>',
        };

        // Phone: digits-only in storage; display as +CC formatted or
        // (XXX) XXX-XXXX for US 10-digit. tel: link uses the digits-only
        // form prefixed with + when the number stored doesn't already
        // include one and starts with anything that looks international.
        function invitadoFormatPhone(raw) {
            const digits = String(raw || '').replace(/^\+/, '');
            const hasPlus = String(raw || '').startsWith('+');
            if (hasPlus) return `+${digits}`;
            if (digits.length === 10) {
                return `(${digits.slice(0, 3)}) ${digits.slice(3, 6)}-${digits.slice(6)}`;
            }
            if (digits.length === 11 && digits.startsWith('1')) {
                return `(${digits.slice(1, 4)}) ${digits.slice(4, 7)}-${digits.slice(7)}`;
            }
            // Some submissions are international without a + (e.g.
            // Sweden as "0046..."). Display as-is rather than guessing.
            return raw;
        }
        function invitadoPhoneHref(raw) {
            const digits = String(raw || '').replace(/[^\d+]/g, '');
            if (!digits) return '#';
            if (digits.startsWith('+')) return `tel:${digits}`;
            // Heuristic: 10-digit numbers are US; everything else gets
            // a leading + because it's almost certainly international
            // (Sweden's "0046..." is the form people typed; e164 wants +46).
            if (digits.length === 10) return `tel:+1${digits}`;
            return `tel:+${digits.replace(/^0+/, '')}`;
        }

        // Each contact platform: how to render its label + how to build
        // the href. Returns null when value is empty so the caller can
        // skip the row entirely.
        function invitadoContactRow(kind, value) {
            if (!value) return null;
            const v = String(value).trim();
            if (!v) return null;
            let label, href;
            switch (kind) {
                case 'phone':
                    label = invitadoFormatPhone(v);
                    href = invitadoPhoneHref(v);
                    break;
                case 'email':
                    label = v;
                    href = `mailto:${v}`;
                    break;
                case 'instagram':
                    label = `@${v}`;
                    href = `https://instagram.com/${encodeURIComponent(v)}`;
                    break;
                case 'linkedin':
                    // Already a canonical https URL post-normalization.
                    // Show a slimmed label so it doesn't run off the line.
                    label = v.replace(/^https?:\/\/(?:www\.)?/, '');
                    href = v;
                    break;
                case 'twitter':
                    label = `@${v}`;
                    href = `https://twitter.com/${encodeURIComponent(v)}`;
                    break;
                case 'bluesky':
                    label = v.startsWith('@') ? v : `@${v}`;
                    href = `https://bsky.app/profile/${encodeURIComponent(v.replace(/^@/, ''))}`;
                    break;
                case 'soundcloud':
                    label = `soundcloud.com/${v}`;
                    href = `https://soundcloud.com/${encodeURIComponent(v)}`;
                    break;
                default:
                    return null;
            }
            return { kind, label, href };
        }

        function invitadoContactsHtml(guest) {
            if (!guest || !guest.hasContacts || !guest.contacts) return '';
            const order = ['phone', 'email', 'instagram', 'linkedin', 'twitter', 'bluesky', 'soundcloud'];
            const rows = order
                .map(k => invitadoContactRow(k, guest.contacts[k]))
                .filter(Boolean);
            if (!rows.length) return '';
            const lis = rows.map(r => `
                <li>
                    <span class="invitado-contact-icon" aria-hidden="true">${INVITADO_CONTACT_ICONS[r.kind] || ''}</span>
                    <a href="${invitadoEscape(r.href)}"${r.kind === 'phone' || r.kind === 'email' ? '' : ' target="_blank" rel="noopener"'}>${invitadoEscape(r.label)}</a>
                </li>
            `).join('');
            return `
                <div class="invitado-contact-label">
                    <svg class="invitado-contact-label-bud" viewBox="0 0 24 24" role="presentation" aria-hidden="true">
                        <path d="M10 17 L9 6 L15 6 L14 17 Z" fill="#F5F0EB" stroke="#1F6E8C" stroke-width="1"/>
                        <rect x="9" y="9.5" width="6" height="3" fill="#E24A2E"/>
                        <rect x="9.5" y="3" width="5" height="4" fill="#1F6E8C"/>
                        <path d="M9 3 L12 0 L15 3 Z" fill="#E24A2E"/>
                    </svg>
                    Stay in Touch
                </div>
                <ul class="invitado-contacts">${lis}</ul>
            `;
        }

        // First N grid cards load eagerly with high priority so the top
        // of the screen paints immediately; the rest lazy-load as the
        // user scrolls. Three columns × three rows ≈ above-the-fold on
        // a typical phone viewport.
        const INVITADO_EAGER_COUNT = 9;

        function invitadoGridPhotoHtml(guest, i) {
            const eager = i < INVITADO_EAGER_COUNT;
            const loading = eager ? 'eager' : 'lazy';
            const fp = eager ? ' fetchpriority="high"' : '';
            if (guest.thumbWebp && guest.thumbJpg) {
                return `<picture>
                    <source type="image/webp" srcset="${invitadoEscape(guest.thumbWebp)}">
                    <img class="avatar-img" src="${invitadoEscape(guest.thumbJpg)}" alt="" loading="${loading}" decoding="async"${fp} onerror="this.parentNode.remove()">
                </picture>`;
            }
            if (guest.photoUrl) {
                return `<img class="avatar-img" src="${invitadoEscape(guest.photoUrl)}" alt="" loading="${loading}" decoding="async"${fp} onerror="this.remove()">`;
            }
            return '';
        }

        function invitadoRenderGrid() {
            const grid = document.getElementById('invitadoGrid');
            grid.innerHTML = '';
            guests.forEach((guest, i) => {
                const colorClass = avatarColors[i % avatarColors.length];
                const photoImg = invitadoGridPhotoHtml(guest, i);
                // Rosa rugosa badge on the thumb when this guest filled
                // the Contact & Socials form. eager-loaded above the
                // fold so it paints in lockstep with the photo it sits
                // on top of; everything else lazy-loads to match the
                // photos' loading policy and not slow the grid render.
                const eager = i < INVITADO_EAGER_COUNT;
                const badgeHtml = guest.hasContacts
                    ? `<div class="invitado-thumb-badge" aria-label="Shared contact info"><img src="images/rosa-rugosa.png" onerror="this.style.display='none'" alt="" loading="${eager ? 'eager' : 'lazy'}"></div>`
                    : '';
                const cell = document.createElement('div');
                cell.className = 'invitado-thumb';
                cell.dataset.index = i;
                cell.onclick = () => invitadoOpenProfile(i);
                cell.innerHTML = `
                    <div class="invitado-thumb-photo ${colorClass}">${invitadoEscape(guest.initials)}${photoImg}${badgeHtml}</div>
                    <div class="invitado-thumb-name">${invitadoEscape(guest.name)}</div>
                `;
                grid.appendChild(cell);
            });
        }

        // Jump from a Schedule event card into Who's Coming with the
        // list pre-filtered to guests attending that welcome dinner.
        // Called from the "Who's coming" button on Thursday/Friday event
        // cards. dayWord is "thursday" or "friday" — it goes straight
        // into the search input, and invitadoFilter() matches it against
        // guest.events ("Thursday Welcome Dinner", etc.).
        function invitadoFilterByEvent(dayWord) {
            switchScreen('facebook', 0);
            // Make sure we land on the browse grid, not a previously
            // opened profile card.
            if (typeof invitadoShowBrowse === 'function') invitadoShowBrowse();
            const input = document.getElementById('invitadoSearch');
            if (input) {
                input.value = dayWord;
                invitadoFilter();
                // Give the user a visual hint that the filter came from
                // elsewhere by briefly focusing the search input.
                input.focus({ preventScroll: true });
            }
        }

        function invitadoSearchClear() {
            const input = document.getElementById('invitadoSearch');
            if (!input) return;
            input.value = '';
            invitadoFilter();
            input.focus({ preventScroll: true });
        }

        function invitadoFilter() {
            const input = document.getElementById('invitadoSearch');
            const q = input.value.trim().toLowerCase();
            const clearBtn = document.getElementById('invitadoSearchClear');
            if (clearBtn) clearBtn.hidden = input.value.length === 0;
            const grid = document.getElementById('invitadoGrid');
            const empty = document.getElementById('invitadoEmpty');
            let visible = 0;
            grid.querySelectorAll('.invitado-thumb').forEach((el) => {
                if (!q) {
                    el.style.display = '';
                    visible++;
                    return;
                }
                const guest = guests[parseInt(el.dataset.index, 10)];
                // guest.events is an array of event names like
                // ["Thursday Welcome Dinner", "Saturday Wedding Ceremony"];
                // when joined into the haystack below it stringifies via
                // Array.toString() (comma-joined), so typing "thursday"
                // or "saturday" still filters down to the right guests.
                // Contact-info values (handles, emails, phone) are also
                // searchable so "type the handle, find the person" works
                // — useful when a guest at the wedding only knows another
                // guest by their Instagram. Object.values yields '' for
                // unset platforms, so the join below stays compact.
                const contactValues = guest.contacts
                    ? Object.values(guest.contacts).join(' ')
                    : '';
                const hay = [
                    guest.firstName,
                    guest.lastName,
                    guest.name,
                    guest.city,
                    guest.hometown,
                    guest.story,
                    guest.leastFavorite,
                    guest.guestMemory,
                    guest.events,
                    contactValues,
                ].join(' ').toLowerCase();
                const match = hay.includes(q);
                el.style.display = match ? '' : 'none';
                if (match) visible++;
            });
            empty.style.display = visible === 0 ? 'block' : 'none';
        }

        function invitadoProfilePhotoHtml(guest) {
            // Prefer the resized -full.webp; show the cached thumb behind it
            // (blurred) so there's no empty avatar while the full loads.
            if (guest.fullWebp && guest.thumbWebp && guest.thumbJpg) {
                return `<picture>
                        <source type="image/webp" srcset="${invitadoEscape(guest.thumbWebp)}">
                        <img class="avatar-img profile-thumb" src="${invitadoEscape(guest.thumbJpg)}" alt="" decoding="async">
                    </picture>
                    <img class="avatar-img profile-full" src="${invitadoEscape(guest.fullWebp)}" alt="" decoding="async" onload="this.classList.add('loaded')" onerror="this.remove()">`;
            }
            if (guest.photoUrl) {
                return `<img class="avatar-img" src="${invitadoEscape(guest.photoUrl)}" alt="" decoding="async" onerror="this.remove()">`;
            }
            return '';
        }

        // Adjacent-guest prefetch: when a profile opens, hint the
        // browser to fetch the next/prev full image so swipe/arrow
        // navigation feels instant. Reuses the same <link rel="prefetch">
        // node — no DOM growth as the user pages through.
        let invitadoPrefetchEl = null;
        function invitadoPrefetchAdjacent(i) {
            if (!guests.length) return;
            const targets = [
                guests[(i + 1) % guests.length],
                guests[(i - 1 + guests.length) % guests.length],
            ];
            targets.forEach((g) => {
                if (!g || !g.fullWebp) return;
                const link = document.createElement('link');
                link.rel = 'prefetch';
                link.as = 'image';
                link.type = 'image/webp';
                link.href = g.fullWebp;
                document.head.appendChild(link);
            });
        }

        function invitadoRenderProfile(i) {
            if (!guests.length) return;
            const guest = guests[i];
            const colorClass = avatarColors[i % avatarColors.length];
            const card = document.getElementById('invitadoCard');
            const photoImg = invitadoProfilePhotoHtml(guest);
            const cityHtml = guest.city
                ? `<div class="invitado-city">${invitadoEscape(guest.city)}</div>`
                : '';
            const hometownHtml = guest.hometown
                ? `<div class="invitado-hometown">Grew up in ${invitadoEscape(guest.hometown)}</div>`
                : '';
            const storyHtml = guest.story
                ? `<div class="invitado-story-label">How I know Hilary and Elliott</div>
                   <div class="invitado-story">${invitadoEscape(guest.story)}</div>`
                : '';
            const leastHtml = guest.leastFavorite
                ? `<div class="invitado-least-label">${invitadoEscape(guest.firstName)}&rsquo;s go-to karaoke song</div>
                   <div class="invitado-least">${invitadoEscape(guest.leastFavorite)}</div>`
                : '';
            const guestMemoryHtml = guest.guestMemory
                ? `<div class="invitado-memory-label">A memory ${invitadoEscape(guest.firstName)} shared</div>
                   <div class="invitado-memory">${invitadoEscape(guest.guestMemory)}</div>`
                : '';
            const memoriesHtml = (guest.memories || []).map(m => `
                <div class="invitado-memory-label">${invitadoEscape(m.title)}</div>
                <div class="invitado-memory">${invitadoEscape(m.text)}</div>
            `).join('');
            const partners = guest.hereWith || [];
            let hereWithHtml = '';
            if (partners.length > 0) {
                const linkHtml = (p) =>
                    `<button class="invitado-herewith-link" data-chip-key="${invitadoEscape(p.key)}">${invitadoEscape(p.name)}</button>`;
                let namesHtml;
                if (partners.length === 1) {
                    namesHtml = linkHtml(partners[0]);
                } else if (partners.length === 2) {
                    namesHtml = `${linkHtml(partners[0])} and ${linkHtml(partners[1])}`;
                } else {
                    const head = partners.slice(0, -1).map(linkHtml).join(', ');
                    namesHtml = `${head}, and ${linkHtml(partners[partners.length - 1])}`;
                }
                hereWithHtml = `<div class="invitado-herewith">Here with ${namesHtml}</div>`;
            }
            // guest.events is an array of event names; render each on
            // its own line so the list reads as a tidy column even when
            // the guest is attending all four.
            const eventsHtml = (guest.events && guest.events.length)
                ? `<div class="invitado-events">
                     <div class="invitado-events-label">Events</div>
                     <div class="invitado-events-body">${guest.events.map(e => `<div class="invitado-events-item">${invitadoEscape(e)}</div>`).join('')}</div>
                   </div>`
                : '';
            // Tiny SMS link so any guest who wants to correct something
            // on their profile can text Elien directly.
            const claimHtml = '';
            // Stay in Touch — only renders if the guest has at least
            // one non-empty contact field (form-submitted OR hand-added
            // by Elien/Nima). Same source-of-truth as the grid badge.
            const contactsHtml = invitadoContactsHtml(guest);
            card.innerHTML = `
                <div class="invitado-photo ${colorClass}">${invitadoEscape(guest.initials)}${photoImg}</div>
                <div class="invitado-name">${invitadoEscape(guest.name)}</div>
                ${guest.pronouns ? `<div class="invitado-city">${invitadoEscape(guest.pronouns)}</div>` : ''}
                ${cityHtml}
                ${hometownHtml}
                ${hereWithHtml}
                ${storyHtml}
                ${leastHtml}
                ${guestMemoryHtml}
                ${memoriesHtml}
                ${eventsHtml}
                ${contactsHtml}
                ${claimHtml}
            `;
            card.querySelectorAll('.invitado-herewith-link').forEach(btn => {
                btn.addEventListener('click', () => {
                    const k = btn.dataset.chipKey;
                    if (k in invitadoKeyToIndex) invitadoOpenProfile(invitadoKeyToIndex[k]);
                    else if (k in partnerProfilesByKey) invitadoOpenPartnerProfile(k);
                });
            });
        }

        function invitadoShowProfile() {
            document.getElementById('invitadoBrowse').style.display = 'none';
            document.getElementById('invitadoProfile').style.display = '';
            document.querySelector('#facebook .header-title').style.display = 'none';
            // Back chevron in the header points "back to the browse grid"
            // when a profile is open. Always shown in this state.
            const backBtn = document.getElementById('invitadoBackBtn');
            if (backBtn) backBtn.style.display = '';
        }

        function invitadoShowBrowse() {
            document.getElementById('invitadoProfile').style.display = 'none';
            document.getElementById('invitadoBrowse').style.display = '';
            document.querySelector('#facebook .header-title').style.display = '';
            invitadoPartnerOnly = false;
            // Who's Coming is now the app's home, so the browse grid has
            // nothing meaningful to back out to — hide the chevron.
            const backBtn = document.getElementById('invitadoBackBtn');
            if (backBtn) backBtn.style.display = 'none';
        }

        function invitadoOpenProfile(i) {
            invitadoIndex = i;
            invitadoPartnerOnly = false;
            invitadoSetNavVisible(true);
            // Reveal the profile container BEFORE injecting images into it.
            // iOS Safari can fail to start the fetch for images inserted
            // into a display:none subtree, leaving us with the blurred
            // -thumb visible (it's already cached from the grid) and the
            // -full webp permanently unloaded.
            invitadoShowProfile();
            invitadoRenderProfile(i);
            invitadoPrefetchAdjacent(i);
            window.scrollTo(0, 0);
            const key = guests[i] && guests[i].key;
            if (key) updateHash('/invitados/' + key);
        }

        function invitadoSetNavVisible(visible) {
            const nav = document.querySelector('#invitadoProfile .invitado-nav');
            if (nav) nav.style.display = visible ? '' : 'none';
        }

        // Open a linked-partner stub — a guest who hasn't filled the form
        // but is tagged as someone's "Here with". Renders a minimal card
        // with just photo + name + a soft "no story yet" note. Does NOT
        // engage swipe nav; the array of guests doesn't include this key.
        let invitadoPartnerOnly = false;
        function invitadoOpenPartnerProfile(key) {
            const profile = partnerProfilesByKey[key];
            if (!profile) return;
            invitadoPartnerOnly = true;
            invitadoSetNavVisible(false);
            invitadoShowProfile();
            invitadoRenderPartnerProfile(profile);
            window.scrollTo(0, 0);
            updateHash('/invitados/' + key);
        }

        function invitadoRenderPartnerProfile(profile) {
            const card = document.getElementById('invitadoCard');
            // Reuse a deterministic color based on the key so partner
            // profiles look stable across reloads even though they're
            // not part of the swipe-indexed array.
            const colorIdx = Math.abs(Array.from(profile.key).reduce((h, c) => (h * 31 + c.charCodeAt(0)) | 0, 0)) % avatarColors.length;
            const colorClass = avatarColors[colorIdx];
            const photoImg = invitadoProfilePhotoHtml(profile);
            card.innerHTML = `
                <div class="invitado-photo ${colorClass}">${invitadoEscape(profile.initials)}${photoImg}</div>
                <div class="invitado-name">${invitadoEscape(profile.name)}</div>
                <div class="invitado-no-story">Hasn&rsquo;t done <a href="https://docs.google.com/forms/d/e/1FAIpQLScKyPDdRyFx7c5XRzwR9hu2zkYkirmau2vHExvlAecMUEsuGA/viewform" target="_blank" rel="noopener">the survey</a> yet.</div>
            `;
        }

        function invitadoBack() {
            // Back chevron in the Who's Coming header. Only meaningful
            // when a profile is open — close it and return to the browse
            // grid. The button is hidden on the browse grid itself
            // (Who's Coming is the app's home, no further "back"); the
            // ESC keybinding still calls this and quietly does nothing
            // when no profile is showing.
            const profileVisible =
                document.getElementById('invitadoProfile').style.display !== 'none';
            if (profileVisible) {
                invitadoShowBrowse();
                window.scrollTo(0, 0);
                updateHash('/invitados');
            }
        }

        function invitadoSurprise() {
            if (!guests.length) return;
            const i = Math.floor(Math.random() * guests.length);
            invitadoOpenProfile(i);
        }

        function invitadoNext() {
            if (!guests.length || invitadoPartnerOnly) return;
            invitadoIndex = (invitadoIndex + 1) % guests.length;
            invitadoRenderProfile(invitadoIndex);
            invitadoPrefetchAdjacent(invitadoIndex);
            const key = guests[invitadoIndex] && guests[invitadoIndex].key;
            if (key) updateHash('/invitados/' + key);
        }

        function invitadoPrev() {
            if (!guests.length || invitadoPartnerOnly) return;
            invitadoIndex = (invitadoIndex - 1 + guests.length) % guests.length;
            invitadoRenderProfile(invitadoIndex);
            invitadoPrefetchAdjacent(invitadoIndex);
            const key = guests[invitadoIndex] && guests[invitadoIndex].key;
            if (key) updateHash('/invitados/' + key);
        }

        let invitadoGridRendered = false;
        function invitadoEnsureGridRendered() {
            if (invitadoGridRendered || !guests.length) return;
            invitadoRenderGrid();
            invitadoGridRendered = true;
        }

        function invitadoInit() {
            if (!guests.length) return;

            // Swipe nav on the profile card
            const card = document.getElementById('invitadoCard');
            let touchStartX = null;
            let touchStartY = null;
            card.addEventListener('touchstart', (e) => {
                touchStartX = e.touches[0].clientX;
                touchStartY = e.touches[0].clientY;
            }, { passive: true });
            card.addEventListener('touchend', (e) => {
                if (touchStartX === null) return;
                const dx = e.changedTouches[0].clientX - touchStartX;
                const dy = e.changedTouches[0].clientY - touchStartY;
                if (Math.abs(dx) > 50 && Math.abs(dx) > Math.abs(dy)) {
                    if (dx < 0) invitadoNext();
                    else invitadoPrev();
                }
                touchStartX = null;
                touchStartY = null;
            }, { passive: true });

            document.addEventListener('keydown', (e) => {
                if (currentScreen !== 'facebook') return;
                const profileVisible = document.getElementById('invitadoProfile').style.display !== 'none';
                if (!profileVisible) return;
                if (e.key === 'ArrowRight') invitadoNext();
                else if (e.key === 'ArrowLeft') invitadoPrev();
                else if (e.key === 'Escape') invitadoBack();
            });
        }

        invitadoInit();

        // Hash router: honor initial deep link (if user is past login) and
        // react to back/forward navigation + manual URL-bar edits.
        window.addEventListener('popstate', () => applyHash());
        window.addEventListener('hashchange', () => applyHash());
        if (!document.getElementById('login').classList.contains('active') &&
            !document.getElementById('splash').classList.contains('active')) {
            applyHash();
        }

        // Service worker + auto-update for home-screen installs.
        // Browsers rarely re-check sw.js on a long-lived standalone window, so
        // we explicitly call registration.update() on load, when the app comes
        // back to focus, and every 30 minutes while open. The SW already calls
        // skipWaiting()+clients.claim(), so when a new version finishes
        // installing it takes control immediately and 'controllerchange'
        // fires; we reload once (guarded against reload loops) so the user
        // sees the fresh content without having to manually close the app.
        //
        // While an update is actually installing on top of an already-
        // controlled page we show a flower overlay so the user has visual
        // feedback during the swap (helps the "I have to reinstall to get
        // updates" pain point). First installs don't show it — there's no
        // existing UI to interrupt and the flash would feel jarring.
        if ('serviceWorker' in navigator) {
            const updateEl = document.getElementById('updateIndicator');
            const showUpdate = () => { if (updateEl) updateEl.classList.add('visible'); };
            const hideUpdate = () => { if (updateEl) updateEl.classList.remove('visible'); };

            let reloading = false;
            navigator.serviceWorker.addEventListener('controllerchange', () => {
                if (reloading) return;
                reloading = true;
                // Keep the flower visible through the reload to avoid a flash
                // of stale content between activation and the new page paint.
                window.location.reload();
            });

            const trackInstalling = (worker) => {
                if (!worker) return;
                // Only surface the flower for *updates*, not first installs.
                if (!navigator.serviceWorker.controller) return;
                showUpdate();
                worker.addEventListener('statechange', () => {
                    if (worker.state === 'redundant') hideUpdate();
                });
            };

            window.addEventListener('load', () => {
                navigator.serviceWorker.register('sw.js').then((registration) => {
                    if (registration.installing) trackInstalling(registration.installing);
                    if (registration.waiting && navigator.serviceWorker.controller) {
                        showUpdate();
                        registration.waiting.postMessage({ type: 'SKIP_WAITING' });
                    }
                    registration.addEventListener('updatefound', () => {
                        trackInstalling(registration.installing);
                    });

                    const checkForUpdate = () => {
                        registration.update().catch(() => {});
                    };
                    document.addEventListener('visibilitychange', () => {
                        if (document.visibilityState === 'visible') checkForUpdate();
                    });
                    setInterval(checkForUpdate, 30 * 60 * 1000);
                }).catch((err) => {
                    console.warn('SW registration failed:', err);
                });
            });
        }

        // Add to Home Screen
        // - Android/Chrome: capture beforeinstallprompt and trigger native dialog.
        //   If the user dismisses, fall back to the iOS-style instruction sheet
        //   (covers cases where the prompt won't fire again this session).
        // - iOS Safari: no JS install API exists, so we always show instructions.
        // - On the login screen the CTA floats; afterwards it lives in the menu.
        let deferredInstallPrompt = null;

        window.addEventListener('beforeinstallprompt', (e) => {
            e.preventDefault();
            deferredInstallPrompt = e;
            updateInstallUi();
        });

        window.addEventListener('appinstalled', () => {
            deferredInstallPrompt = null;
            updateInstallUi();
        });

        function isStandaloneApp() {
            return window.matchMedia('(display-mode: standalone)').matches ||
                   window.navigator.standalone === true;
        }

        function isIOSDevice() {
            return /iPhone|iPad|iPod/.test(navigator.userAgent) && !window.MSStream;
        }

        function isAndroidDevice() {
            return /Android/.test(navigator.userAgent);
        }

        function canShowInstall() {
            if (isStandaloneApp()) return false;
            if (deferredInstallPrompt) return true;
            if (isIOSDevice()) return true;
            return false;
        }

        function updateInstallUi() {
            const cta = document.getElementById('installCta');
            const menuItem = document.getElementById('installMenuItem');
            const show = canShowInstall();
            const loginEl = document.getElementById('login');
            const loginActive = loginEl ? loginEl.classList.contains('active') : false;
            if (cta) cta.classList.toggle('visible', show && loginActive);
            if (menuItem) menuItem.classList.toggle('visible', show && !loginActive);
        }

        async function installApp() {
            if (deferredInstallPrompt) {
                const promptEvent = deferredInstallPrompt;
                deferredInstallPrompt = null;
                try {
                    promptEvent.prompt();
                    const choice = await promptEvent.userChoice;
                    if (choice && choice.outcome === 'accepted') {
                        updateInstallUi();
                        return;
                    }
                } catch (err) {
                    console.warn('Install prompt failed:', err);
                }
                showInstallInstructions();
                updateInstallUi();
                return;
            }
            showInstallInstructions();
        }

        function showInstallInstructions() {
            closeMenu();
            const ios = document.getElementById('installSheetIos');
            const android = document.getElementById('installSheetAndroid');
            const generic = document.getElementById('installSheetGeneric');
            ios.style.display = 'none';
            android.style.display = 'none';
            generic.style.display = 'none';
            if (isIOSDevice()) ios.style.display = '';
            else if (isAndroidDevice()) android.style.display = '';
            else generic.style.display = '';
            document.getElementById('installInstructions').classList.add('active');
        }

        function hideInstallInstructions() {
            document.getElementById('installInstructions').classList.remove('active');
        }

        updateInstallUi();
    </script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
