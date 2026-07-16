#!/usr/bin/env python3
"""Generate a printable VISUAL FLOOR-PLAN PDF of the Saturday seating
chart for the wedding planner. Companion to gen_seating_pdf.py (which
writes the list-style PDF). Pulls live data from wedding.db and writes
seating_chart_saturday_map.pdf at the repo root.

Goal: a single Tabloid-portrait sheet a planner can walk the room with.
Each table card shows the T#, group name, and the per-table food
breakdown so a server delivering plates can find a table by number and
verify counts at a glance.

The layout mirrors the .layout-instituto grid in scripts/seating_app.html
so the printed map matches what Elien sees in the live editor.

Run from the repo root:
    python3 scripts/gen_seating_map_pdf.py
"""
import json
import math
import os
import sqlite3
from datetime import date

from reportlab.lib.colors import HexColor
from reportlab.pdfgen import canvas


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
DB_PATH = os.path.join(REPO_ROOT, "wedding.db")
OUT_PATH = os.path.join(REPO_ROOT, "seating_chart_saturday_map.pdf")

DAY = "sat"

INK = HexColor("#2b2b2b")
INK_SOFT = HexColor("#6b6b6b")
INK_FAINT = HexColor("#9a9a9a")
RULE = HexColor("#d8d2c4")
PAPER = HexColor("#fffdf6")
TABLE_FILL = HexColor("#fffdf9")
TABLE_STROKE = HexColor("#b8a98a")
RAISED_FILL = HexColor("#fdf5e8")
RAISED_STROKE = HexColor("#d8c8b2")
DANCE_FILL = HexColor("#e8d4b8")
DANCE_STROKE = HexColor("#b89574")
DJ_FILL = HexColor("#c4a478")
DJ_INK = HexColor("#3a2815")
COCINA_FILL = HexColor("#e8e0d4")
COCINA_STROKE = HexColor("#a0967f")
BAR_FILL = HexColor("#d4c4a8")
BAR_INK = HexColor("#4a3826")
ENTR_FILL = HexColor("#efe7d6")
RESTR_FILL = HexColor("#ece5d3")
STEP_INK = HexColor("#8a7a60")

# Food color buckets — same palette as gen_seating_pdf.py and the live editor
FOOD_COLORS = {
    "Fish":   HexColor("#4a7a9c"),
    "Kebab":  HexColor("#b07242"),
    "Chille": HexColor("#8a3e3e"),
    "Pasta":  HexColor("#a88a3a"),
    "Other":  HexColor("#5a6b5e"),
}
FOOD_ORDER = ["Fish", "Kebab", "Chille", "Pasta", "Other"]


# Grid template (mirrors .layout-instituto in seating_app.html). Each
# entry is (col, row, col_span, row_span). Col 0 .. 5 left→right;
# row 0 .. 15 top→bottom (top of grid = north of room = cocina end).
AREAS = {
    "bar2":   (1,  0, 1, 1),
    "t20":    (3,  0, 1, 1),
    "cocina": (5,  0, 1, 4),
    "t17":    (2,  1, 1, 1),
    "t18":    (3,  1, 1, 1),
    "t19":    (4,  1, 1, 1),
    "t14":    (2,  2, 1, 1),
    "t15":    (3,  2, 1, 1),
    "t16":    (4,  2, 1, 1),
    "t12":    (2,  3, 1, 1),
    "t13":    (3,  3, 1, 1),  # long table — kids
    "restr":  (0,  4, 1, 1),
    "stepup": (1,  5, 4, 1),
    "t9":     (2,  6, 1, 1),
    "t10":    (3,  6, 1, 1),
    "t11":    (4,  6, 1, 1),
    "stepdn": (1,  7, 4, 1),
    "t8":     (2,  8, 1, 1),
    "t7":     (4,  8, 1, 1),
    "dj":     (5,  8, 1, 2),
    "t6":     (3,  9, 1, 1),
    "t5":     (1, 10, 1, 2),  # long table — west wall upper
    "dance":  (2, 10, 3, 4),
    "t4":     (1, 12, 1, 2),  # long table — west wall lower
    "t1":     (2, 14, 1, 1),
    "t2t3":   (3, 14, 2, 1),  # long table — south wall (Big Persians, T1)
    "entr":   (1, 15, 4, 1),
}

# Column / row weights — chosen so SQUARE units (1 col-unit == 1 row-unit
# in points) produce a sensible-looking floor plan. Cols 2-4 are the
# table columns; col 0 a narrow restroom strip; col 5 the cocina/DJ strip.
COL_W = [0.5, 2.1, 3.2, 3.2, 3.2, 1.0]
ROW_H = [
    1.7,  # row 0  (bar2 / t20)
    1.7,  # row 1  (t17 / t18 / t19)
    1.7,  # row 2  (t14 / t15 / t16)
    1.7,  # row 3  (t12 / t13)
    0.6,  # row 4  (restr line)
    0.4,  # row 5  (stepup band)
    1.9,  # row 6  (t9 / t10 / t11 — raised middle)
    0.4,  # row 7  (stepdn band)
    1.7,  # row 8  (t8 / t7 — front north)
    1.5,  # row 9  (t6)
    1.0,  # row 10 (t5 top half / dance)
    1.0,  # row 11 (t5 bottom half / dance)
    1.0,  # row 12 (t4 top half / dance)
    1.0,  # row 13 (t4 bottom half / dance)
    1.7,  # row 14 (t1 / t2t3 — south)
    0.5,  # row 15 (entrance)
]

LONG_VERTICAL = {"t4", "t5"}
LONG_HORIZONTAL = {"t13", "t2t3"}
RAISED_AREAS = {"t9", "t10", "t11"}


def classify_food(raw):
    if not raw:
        return None
    lower = raw.lower().strip()
    if lower.startswith("fish"):
        return "Fish"
    if lower.startswith("kebab"):
        return "Kebab"
    if lower.startswith("chille"):
        return "Chille"
    if lower.startswith("plain pasta") or lower.startswith("pasta"):
        return "Pasta"
    return "Other"


def fetch_data():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    tables = con.execute(
        """
        SELECT table_key, table_num, name, capacity, sort_order, layout_json
        FROM seating_tables
        WHERE day = ?
        ORDER BY sort_order, table_num
        """,
        (DAY,),
    ).fetchall()

    food_rows = con.execute(
        """
        SELECT sa.table_key, g.food_choice
        FROM seating_assignments sa
        JOIN guests g ON g.id = sa.guest_id
        WHERE sa.day = ?
        """,
        (DAY,),
    ).fetchall()
    con.close()

    # Per-table seated count + food breakdown
    seated = {}
    foods = {}
    for r in food_rows:
        key = r["table_key"]
        seated[key] = seated.get(key, 0) + 1
        bucket = classify_food(r["food_choice"])
        if bucket is None:
            continue
        foods.setdefault(key, {})
        foods[key][bucket] = foods[key].get(bucket, 0) + 1
    return tables, seated, foods


def area_of(table_row):
    layout = table_row["layout_json"] or "{}"
    try:
        return json.loads(layout).get("area")
    except json.JSONDecodeError:
        return None


def fit_text(c, text, max_width, font, start_size, min_size=6):
    """Return the largest size in [min_size, start_size] at which `text`
    fits within max_width."""
    size = start_size
    while size > min_size and c.stringWidth(text, font, size) > max_width:
        size -= 0.5
    return size


def main():
    tables, seated_counts, foods_by_key = fetch_data()
    if not tables:
        raise SystemExit("No Saturday tables found in wedding.db")

    by_area = {area_of(t): t for t in tables if area_of(t)}

    # Tabloid PORTRAIT: 11 x 17 inch
    PAGE_W, PAGE_H = 11 * 72, 17 * 72  # 792 x 1224
    c = canvas.Canvas(OUT_PATH, pagesize=(PAGE_W, PAGE_H))
    c.setTitle("Saturday Seating Map — Elien & Nima")
    c.setAuthor("Elien & Nima")

    # Background
    c.setFillColor(PAPER)
    c.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)

    # Title block at top
    margin_x = 30
    title_top = PAGE_H - 30
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 22)
    c.drawCentredString(PAGE_W / 2, title_top, "Wedding Seating Map — Saturday")
    c.setFillColor(INK_SOFT)
    c.setFont("Helvetica", 11)
    c.drawCentredString(
        PAGE_W / 2,
        title_top - 18,
        f"Generated {date.today().isoformat()} · Elien & Nima · Instituto Allende",
    )
    c.setFont("Helvetica-Oblique", 9)
    c.drawCentredString(
        PAGE_W / 2,
        title_top - 32,
        "Top of plan = cocina (north) · Bottom = entrance (south) · Numbering flows CCW from T1",
    )

    # Compute available grid bbox below the title block
    margin_top = 80
    margin_bottom = 90  # leaves room for legend at bottom
    avail_x = margin_x
    avail_y = margin_bottom
    avail_w = PAGE_W - 2 * margin_x
    avail_h = PAGE_H - margin_top - margin_bottom

    col_total = sum(COL_W)
    row_total = sum(ROW_H)
    # SQUARE units: pick the smaller scale so the grid fits both axes
    unit = min(avail_w / col_total, avail_h / row_total)
    grid_w = unit * col_total
    grid_h = unit * row_total
    # Center the grid in the available area
    grid_left = avail_x + (avail_w - grid_w) / 2
    grid_top = avail_y + (avail_h + grid_h) / 2

    col_x = [grid_left]
    for w in COL_W:
        col_x.append(col_x[-1] + w * unit)
    row_y = [grid_top]
    for h in ROW_H:
        row_y.append(row_y[-1] - h * unit)

    GAP = 5

    def cell_bbox(area_code):
        col, row, cs, rs = AREAS[area_code]
        x = col_x[col] + GAP / 2
        x2 = col_x[col + cs] - GAP / 2
        y_top = row_y[row] - GAP / 2
        y_bot = row_y[row + rs] + GAP / 2
        return (x, y_bot, x2 - x, y_top - y_bot)

    # ---- non-table features ----

    # Cocina
    x, y, w, h = cell_bbox("cocina")
    c.setFillColor(COCINA_FILL)
    c.setStrokeColor(COCINA_STROKE)
    c.setLineWidth(0.8)
    c.roundRect(x, y, w, h, 6, fill=1, stroke=1)
    c.saveState()
    c.translate(x + w / 2, y + h / 2)
    c.rotate(90)
    c.setFillColor(INK_SOFT)
    c.setFont("Helvetica-Bold", 14)
    c.drawCentredString(0, -4, "COCINA")
    c.restoreState()

    # DJ
    x, y, w, h = cell_bbox("dj")
    c.setFillColor(DJ_FILL)
    c.setStrokeColor(DJ_FILL)
    c.roundRect(x, y, w, h, 6, fill=1, stroke=1)
    c.saveState()
    c.translate(x + w / 2, y + h / 2)
    c.rotate(90)
    c.setFillColor(DJ_INK)
    c.setFont("Helvetica-Bold", 16)
    c.drawCentredString(0, -5, "DJ")
    c.restoreState()

    # Bar2
    x, y, w, h = cell_bbox("bar2")
    bar_w = min(w, 56)
    c.setFillColor(BAR_FILL)
    c.setStrokeColor(BAR_FILL)
    c.roundRect(x + (w - bar_w) / 2, y, bar_w, h, 4, fill=1, stroke=1)
    c.setFillColor(BAR_INK)
    c.setFont("Helvetica-Bold", 10)
    c.drawCentredString(x + w / 2, y + h / 2 - 3, "BAR")

    # Restrooms
    x, y, w, h = cell_bbox("restr")
    c.setFillColor(RESTR_FILL)
    c.setStrokeColor(COCINA_STROKE)
    c.setLineWidth(0.5)
    c.roundRect(x, y, w, h, 4, fill=1, stroke=1)
    c.setFillColor(INK_SOFT)
    c.setFont("Helvetica-Bold", 8)
    c.drawCentredString(x + w / 2, y + h / 2 - 2, "WC")

    # Step bands
    for area, label in (("stepup", "STEP UP"), ("stepdn", "STEP DOWN")):
        x, y, w, h = cell_bbox(area)
        c.setStrokeColor(RULE)
        c.setDash(4, 3)
        c.setLineWidth(0.8)
        c.line(x, y + h / 2, x + w, y + h / 2)
        c.setDash()
        c.setFillColor(STEP_INK)
        c.setFont("Helvetica-Bold", 8)
        c.drawCentredString(x + w / 2, y + h / 2 - 3, label)

    # Entrance
    x, y, w, h = cell_bbox("entr")
    c.setFillColor(ENTR_FILL)
    c.setStrokeColor(COCINA_STROKE)
    c.setLineWidth(0.5)
    c.roundRect(x, y, w, h, 4, fill=1, stroke=1)
    c.setFillColor(INK_SOFT)
    c.setFont("Helvetica-Bold", 11)
    c.drawCentredString(x + w / 2, y + h / 2 - 4, "ENTRANCE  ↓")

    # Dance floor — anchor of the room
    x, y, w, h = cell_bbox("dance")
    c.setFillColor(DANCE_FILL)
    c.setStrokeColor(DANCE_STROKE)
    c.setLineWidth(1.2)
    c.roundRect(x, y, w, h, 10, fill=1, stroke=1)
    # Hatch stripes
    c.saveState()
    p = c.beginPath()
    p.moveTo(x, y)
    p.lineTo(x + w, y)
    p.lineTo(x + w, y + h)
    p.lineTo(x, y + h)
    p.close()
    c.clipPath(p, stroke=0, fill=0)
    c.setStrokeColor(HexColor("#d4b896"))
    c.setLineWidth(0.5)
    diag = math.hypot(w, h)
    step_px = 16
    for i in range(int(-diag), int(diag * 2), step_px):
        c.line(x + i, y, x + i + h, y + h)
    c.restoreState()
    # BIG label, fit-to-width with letter-spacing
    c.setFillColor(HexColor("#5c4733"))
    label = "DANCE FLOOR"
    label_size = 64
    while c.stringWidth(label, "Helvetica-Bold", label_size) > w * 0.85 and label_size > 28:
        label_size -= 2
    spaced = "  ".join(list(label))
    if c.stringWidth(spaced, "Helvetica-Bold", label_size) <= w * 0.92:
        label = spaced
    c.setFont("Helvetica-Bold", label_size)
    c.drawCentredString(x + w / 2, y + h / 2 - label_size / 3, label)

    # ---- tables: rounded-rect cards with T#, name, food breakdown ----

    def draw_food_pills(c, cx, baseline_y, table_key, max_w, font_size=9):
        """Draw the food breakdown as colored count pills. Falls back to
        single-letter codes (F/K/C/P/O) when the card is too narrow for
        full names, so the long west-wall tables stay readable."""
        breakdown = foods_by_key.get(table_key) or {}
        parts = []
        for word in FOOD_ORDER:
            n = breakdown.get(word, 0)
            if n > 0:
                parts.append((word, n))
        if not parts:
            c.setFont("Helvetica-Oblique", font_size)
            c.setFillColor(INK_FAINT)
            c.drawCentredString(cx, baseline_y, "no picks yet")
            return

        def measure(parts_, label_fn, fs):
            w = 0
            for word, n in parts_:
                w += c.stringWidth(label_fn(word, n), "Helvetica-Bold", fs)
            w += (len(parts_) - 1) * (fs * 0.7)
            return w

        full_label = lambda word, n: f"{word} {n}"
        short_label = lambda word, n: f"{word[0]}{n}"

        label_fn = full_label
        size = font_size
        # Try full labels first, shrinking; if still over, switch to short
        while size >= 7 and measure(parts, full_label, size) > max_w:
            size -= 0.5
        if measure(parts, full_label, size) > max_w:
            label_fn = short_label
            size = font_size
            while size >= 7 and measure(parts, short_label, size) > max_w:
                size -= 0.5

        text_w = measure(parts, label_fn, size)
        cursor = cx - text_w / 2
        gap = size * 0.7
        for word, n in parts:
            color = FOOD_COLORS[word]
            seg = label_fn(word, n)
            seg_w = c.stringWidth(seg, "Helvetica-Bold", size)
            c.setFillColor(color)
            c.setFont("Helvetica-Bold", size)
            c.drawString(cursor, baseline_y, seg)
            cursor += seg_w + gap

    def draw_table_card_horizontal(area, t, cell_x, cell_y, cell_w, cell_h):
        """Standard horizontal card: T# top-left big, name center, food pills bottom."""
        # Pad inside cell
        pad = 4
        x = cell_x + pad
        y = cell_y + pad
        w = cell_w - 2 * pad
        h = cell_h - 2 * pad

        if area in RAISED_AREAS:
            fill, stroke = RAISED_FILL, RAISED_STROKE
            # Soft halo
            c.setFillColor(HexColor("#e8dabe"))
            c.roundRect(x, y - 2, w, h, 8, fill=1, stroke=0)
        else:
            fill, stroke = TABLE_FILL, TABLE_STROKE

        c.setFillColor(fill)
        c.setStrokeColor(stroke)
        c.setLineWidth(1.2)
        c.roundRect(x, y, w, h, 8, fill=1, stroke=1)

        # Big table number top-left
        num_str = f"T{t['table_num']}"
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 28)
        c.drawString(x + 8, y + h - 26, num_str)

        # Capacity top-right
        cap_str = f"{seated_counts.get(t['table_key'], 0)}/{t['capacity']}"
        c.setFillColor(INK_FAINT)
        c.setFont("Helvetica", 9)
        c.drawRightString(x + w - 8, y + h - 14, cap_str)

        # Name centered (vertically below T#, fit-to-width)
        name = (t["name"] or "").strip()
        name_y = y + h / 2 - 6
        if name:
            name_size = fit_text(c, name, w - 16, "Helvetica", start_size=11, min_size=7)
            c.setFont("Helvetica", name_size)
            c.setFillColor(INK)
            c.drawCentredString(x + w / 2, name_y, name)

        # Food pills along bottom
        draw_food_pills(c, x + w / 2, y + 8, t["table_key"], w - 12, font_size=9)

    # Draw all tables — every card uses the same horizontal layout. Long
    # vertical (west-wall) cells just produce a tall narrow card; the
    # food-pill shrinker falls back to single-letter codes when needed.
    for area, t in by_area.items():
        x, y, w, h = cell_bbox(area)
        draw_table_card_horizontal(area, t, x, y, w, h)

    # ---- legend at the bottom ----

    legend_y = 50
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(margin_x, legend_y + 14, "Food breakdown per table:")

    # Color swatches
    cursor_x = margin_x
    c.setFont("Helvetica-Bold", 10)
    for word in FOOD_ORDER:
        color = FOOD_COLORS[word]
        c.setFillColor(color)
        c.drawString(cursor_x, legend_y, word)
        cursor_x += c.stringWidth(word, "Helvetica-Bold", 10) + 18
    # "no picks yet" indicator
    c.setFillColor(INK_FAINT)
    c.setFont("Helvetica-Oblique", 10)
    c.drawString(cursor_x, legend_y, "no picks yet")

    # Stats footer
    total_seats = sum(t["capacity"] for t in tables)
    total_seated = sum(seated_counts.values())
    food_totals = {}
    for fb in foods_by_key.values():
        for k, v in fb.items():
            food_totals[k] = food_totals.get(k, 0) + v
    food_summary = " · ".join(
        f"{w} {food_totals.get(w, 0)}" for w in FOOD_ORDER if food_totals.get(w, 0) > 0
    )
    c.setFillColor(INK_SOFT)
    c.setFont("Helvetica", 9)
    c.drawRightString(
        PAGE_W - margin_x,
        legend_y + 14,
        f"{len(tables)} tables · {total_seated}/{total_seats} seated",
    )
    c.drawRightString(
        PAGE_W - margin_x,
        legend_y,
        food_summary,
    )

    c.showPage()
    c.save()
    print(f"Wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
