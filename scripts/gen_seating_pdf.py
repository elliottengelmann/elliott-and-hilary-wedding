#!/usr/bin/env python3
"""Generate a printable PDF of the Saturday seating chart for the
wedding planner. Pulls live data from wedding.db and writes
seating_chart_saturday.pdf at the repo root.

Run from the repo root:
    python3 scripts/gen_seating_pdf.py
"""
import json
import os
import sqlite3
from datetime import date

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
DB_PATH = os.path.join(REPO_ROOT, "wedding.db")
OUT_PATH = os.path.join(REPO_ROOT, "seating_chart_saturday.pdf")

DAY = "sat"
DAY_LABEL = "Saturday — Wedding Day"

# Same palette used in scripts/seating_app.html so the PDF matches
# the live editor at a glance.
FOOD_COLORS = {
    "Fish":   colors.HexColor("#4a7a9c"),
    "Kebab":  colors.HexColor("#b07242"),
    "Chille": colors.HexColor("#8a3e3e"),
    "Pasta":  colors.HexColor("#a88a3a"),
    "Other":  colors.HexColor("#5a6b5e"),
}
INK = colors.HexColor("#2b2b2b")
INK_SOFT = colors.HexColor("#6b6b6b")
RULE = colors.HexColor("#d8d2c4")
CREAM = colors.HexColor("#faf6ec")


def classify_food(raw):
    """Return (display_word, full_label). Mirrors foodInfo() in
    seating_app.html so the planner sees the same buckets."""
    if not raw:
        return (None, "")
    lower = raw.lower().strip()
    if lower.startswith("fish"):
        return ("Fish", raw)
    if lower.startswith("kebab"):
        return ("Kebab", raw)
    if lower.startswith("chille"):
        return ("Chille", raw)
    if lower.startswith("plain pasta") or lower.startswith("pasta"):
        return ("Pasta", raw)
    return ("Other", raw)


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

    assigns = con.execute(
        """
        SELECT sa.table_key, sa.seat_idx,
               g.id, g.first_name, g.last_name, g.food_choice
        FROM seating_assignments sa
        JOIN guests g ON g.id = sa.guest_id
        WHERE sa.day = ?
        ORDER BY sa.table_key, sa.seat_idx
        """,
        (DAY,),
    ).fetchall()
    con.close()

    by_table = {}
    for row in assigns:
        by_table.setdefault(row["table_key"], []).append(row)
    return tables, by_table


def food_tag(word, full_label):
    """Inline pill rendered as a colored Paragraph fragment."""
    if word is None:
        return Paragraph(
            '<font color="#9a9a9a"><i>no pick</i></font>',
            BODY_STYLE,
        )
    color = FOOD_COLORS.get(word, FOOD_COLORS["Other"])
    title = ""
    # If the raw label differs from the bucket word (e.g. the long
    # custom Fish-rice note), surface it in italics next to the tag.
    extra = ""
    if full_label and full_label.strip().lower() != word.lower():
        if len(full_label) > 60:
            extra = f' <font color="#6b6b6b" size="8"><i>({full_label[:57]}…)</i></font>'
        else:
            extra = f' <font color="#6b6b6b" size="8"><i>({full_label})</i></font>'
    return Paragraph(
        f'<font color="{"#" + color.hexval()[2:]}"><b>{word}</b></font>{extra}',
        BODY_STYLE,
    )


def build_cover(tables, by_table):
    """Title + overall stats."""
    story = []
    story.append(Spacer(1, 0.6 * inch))
    story.append(Paragraph("Wedding Seating Chart", TITLE_STYLE))
    story.append(Paragraph(DAY_LABEL, SUBTITLE_STYLE))
    story.append(Spacer(1, 0.15 * inch))
    story.append(
        Paragraph(
            f"Generated {date.today().isoformat()} · Elien &amp; Nima",
            META_STYLE,
        )
    )
    story.append(Spacer(1, 0.5 * inch))

    # Aggregate stats
    total_capacity = sum(t["capacity"] for t in tables)
    total_assigned = sum(len(rows) for rows in by_table.values())
    food_counts = {"Fish": 0, "Kebab": 0, "Chille": 0, "Pasta": 0, "Other": 0, "no pick": 0}
    for rows in by_table.values():
        for r in rows:
            word, _ = classify_food(r["food_choice"])
            if word is None:
                food_counts["no pick"] += 1
            else:
                food_counts[word] += 1

    # Summary table
    summary_rows = [
        ["Tables", str(len(tables))],
        ["Total seats", str(total_capacity)],
        ["Seated guests", str(total_assigned)],
        ["Empty seats", str(total_capacity - total_assigned)],
    ]
    t = Table(summary_rows, colWidths=[2.4 * inch, 1.4 * inch], hAlign="CENTER")
    t.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 12),
                ("TEXTCOLOR", (0, 0), (0, -1), INK_SOFT),
                ("TEXTCOLOR", (1, 0), (1, -1), INK),
                ("FONTNAME", (1, 0), (1, -1), "Helvetica-Bold"),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("LINEBELOW", (0, 0), (-1, -2), 0.4, RULE),
            ]
        )
    )
    story.append(t)
    story.append(Spacer(1, 0.4 * inch))

    # Food tally
    story.append(Paragraph("Food picks", H2_STYLE))
    story.append(Spacer(1, 0.1 * inch))
    food_rows = []
    for word in ["Fish", "Kebab", "Chille", "Pasta", "Other"]:
        if food_counts[word] == 0:
            continue
        color = FOOD_COLORS[word]
        food_rows.append([
            Paragraph(
                f'<font color="{"#" + color.hexval()[2:]}"><b>{word}</b></font>',
                BODY_STYLE,
            ),
            str(food_counts[word]),
        ])
    if food_counts["no pick"] > 0:
        food_rows.append([
            Paragraph(
                '<font color="#9a9a9a"><i>no pick</i></font>',
                BODY_STYLE,
            ),
            str(food_counts["no pick"]),
        ])
    ft = Table(food_rows, colWidths=[2.4 * inch, 1.4 * inch], hAlign="CENTER")
    ft.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 12),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("FONTNAME", (1, 0), (1, -1), "Helvetica-Bold"),
                ("TEXTCOLOR", (1, 0), (1, -1), INK),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("LINEBELOW", (0, 0), (-1, -2), 0.4, RULE),
            ]
        )
    )
    story.append(ft)
    story.append(PageBreak())
    return story


def build_table_section(t, rows):
    """One table: heading + seat rows. Wrapped in KeepTogether so it
    doesn't split mid-table across pages when it fits."""
    flow = []
    name = (t["name"] or "").strip()
    heading = f"Table {t['table_num']}"
    if name:
        heading += f" · {name}"
    flow.append(Paragraph(heading, H1_STYLE))

    # Per-table food tally line
    by_food = {"Fish": 0, "Kebab": 0, "Chille": 0, "Pasta": 0, "Other": 0}
    no_pick = 0
    for r in rows:
        word, _ = classify_food(r["food_choice"])
        if word is None:
            no_pick += 1
        else:
            by_food[word] += 1
    tally_parts = []
    for word in ["Fish", "Kebab", "Chille", "Pasta", "Other"]:
        if by_food[word] == 0:
            continue
        color = FOOD_COLORS[word]
        tally_parts.append(
            f'<font color="{"#" + color.hexval()[2:]}"><b>{word}</b></font> {by_food[word]}'
        )
    if no_pick > 0:
        tally_parts.append(
            f'<font color="#9a9a9a"><i>{no_pick} no pick</i></font>'
        )
    cap_line = f"Capacity {t['capacity']} · Seated {len(rows)}"
    if tally_parts:
        cap_line += " · " + " &nbsp; ".join(tally_parts)
    flow.append(Paragraph(cap_line, META_STYLE))
    flow.append(Spacer(1, 0.12 * inch))

    # Seat list — render every seat slot 1..capacity, even unfilled
    seated_by_idx = {r["seat_idx"]: r for r in rows}
    data = [["Seat #", "Guest", "Food"]]
    for i in range(t["capacity"]):
        r = seated_by_idx.get(i)
        if r is None:
            data.append([
                str(i + 1),
                Paragraph(
                    '<font color="#bdbdbd"><i>— empty —</i></font>',
                    BODY_STYLE,
                ),
                "",
            ])
        else:
            full = f"{r['first_name'] or ''} {r['last_name'] or ''}".strip()
            word, label = classify_food(r["food_choice"])
            data.append([str(i + 1), Paragraph(full, BODY_STYLE), food_tag(word, label)])

    seat_table = Table(
        data,
        colWidths=[0.7 * inch, 3.3 * inch, 2.7 * inch],
        repeatRows=1,
    )
    seat_table.setStyle(
        TableStyle(
            [
                # Header row
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, 0), 9),
                ("TEXTCOLOR", (0, 0), (-1, 0), INK_SOFT),
                ("BACKGROUND", (0, 0), (-1, 0), CREAM),
                ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
                ("TOPPADDING", (0, 0), (-1, 0), 6),
                ("LINEBELOW", (0, 0), (-1, 0), 0.6, RULE),
                # Body rows
                ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 1), (-1, -1), 11),
                ("ALIGN", (0, 0), (0, -1), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TEXTCOLOR", (0, 1), (0, -1), INK_SOFT),
                ("LINEBELOW", (0, 1), (-1, -2), 0.25, RULE),
                ("BOTTOMPADDING", (0, 1), (-1, -1), 5),
                ("TOPPADDING", (0, 1), (-1, -1), 5),
                ("LEFTPADDING", (1, 0), (-1, -1), 8),
            ]
        )
    )
    flow.append(seat_table)
    flow.append(Spacer(1, 0.25 * inch))
    return flow


# ---- styles -----------------------------------------------------------

styles = getSampleStyleSheet()
TITLE_STYLE = ParagraphStyle(
    "title",
    parent=styles["Title"],
    fontName="Helvetica-Bold",
    fontSize=28,
    textColor=INK,
    alignment=TA_CENTER,
    spaceAfter=4,
)
SUBTITLE_STYLE = ParagraphStyle(
    "subtitle",
    parent=styles["Normal"],
    fontName="Helvetica",
    fontSize=14,
    textColor=INK_SOFT,
    alignment=TA_CENTER,
    spaceAfter=2,
)
META_STYLE = ParagraphStyle(
    "meta",
    parent=styles["Normal"],
    fontName="Helvetica",
    fontSize=10,
    textColor=INK_SOFT,
    alignment=TA_CENTER,
    spaceAfter=2,
)
H1_STYLE = ParagraphStyle(
    "h1",
    parent=styles["Heading1"],
    fontName="Helvetica-Bold",
    fontSize=18,
    textColor=INK,
    spaceBefore=2,
    spaceAfter=2,
    keepWithNext=True,
)
H2_STYLE = ParagraphStyle(
    "h2",
    parent=styles["Heading2"],
    fontName="Helvetica-Bold",
    fontSize=13,
    textColor=INK,
    alignment=TA_CENTER,
    spaceAfter=4,
)
BODY_STYLE = ParagraphStyle(
    "body",
    parent=styles["Normal"],
    fontName="Helvetica",
    fontSize=11,
    textColor=INK,
    leading=14,
)


def main():
    tables, by_table = fetch_data()
    if not tables:
        raise SystemExit("No Saturday tables found in wedding.db")

    doc = SimpleDocTemplate(
        OUT_PATH,
        pagesize=letter,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
        title="Saturday Seating Chart — Elien & Nima",
        author="Elien & Nima",
    )

    story = []
    story.extend(build_cover(tables, by_table))

    for i, t in enumerate(tables):
        rows = by_table.get(t["table_key"], [])
        section = build_table_section(t, rows)
        # Try to keep each table on one page when it fits.
        story.append(KeepTogether(section))

    def _footer(canvas, doc_):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(INK_SOFT)
        text = f"Saturday seating · page {doc_.page}"
        canvas.drawCentredString(letter[0] / 2, 0.4 * inch, text)
        canvas.restoreState()

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    print(f"Wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
