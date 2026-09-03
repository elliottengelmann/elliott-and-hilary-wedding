# CLAUDE.md — Hilary & Elliot's Wedding App

Guidance for Claude Code working in this repository. Read this fully before
making changes. This project was adapted from a template built for another
wedding, so watch for anything still worded for the old couple.

## What this is

A static, installable Progressive Web App (PWA) for **Hilary & Elliot's**
wedding. It's a single `index.html` **generated** by `build.py` from a big
`TEMPLATE` string plus data in `wedding.db` (SQLite). Hosted on Vercel; pushing
to the `main` branch deploys it live automatically. No server, no runtime backend.

The owners are **non-technical** — explain things in plain English, give exact
steps, and prefer doing the work yourself over handing them commands. See the
global instructions for the full plain-language contract.

## Scope — what this app deliberately does NOT have

Two features from the original template were intentionally removed. **Do not
add them back** unless the owners explicitly ask:

- **No seating chart.** No `edit_seating.py`, no `seating_*` tables, no seating UI.
- **No RSVPs.** No attendance tracking, no RSVP-gated schedule, no "Who's Coming."
  Every guest sees the full schedule. The guest directory is driven purely by the
  form: **anyone who fills out the Google Form gets a profile.**

## The guest form and how fields map

Guests fill a Google Form. Its columns map into the app like this (see
`merge_guests.py`, function that reads the form):

| Form question | App field | Shown as |
|---|---|---|
| First Name / Last Name | `first_name` / `last_name` | the guest's name |
| Pronouns | `pronouns` | small line under the name |
| How do you know the couple? | `how_we_know` | "How I know Hilary and Elliot" |
| What is one of your go-to karaoke songs? | `least_favorite` *(reused column)* | "[Name]'s go-to karaoke song" |
| Share a photo… | `photo_url` | profile photo |

**Important quirk:** the karaoke answer is stored in the column named
`least_favorite` (a leftover column name from the template) and the profile label
is set to karaoke in `build.py`. If you ever tidy this up, rename the column
consistently across `merge_guests.py`, `build.py`, `scripts/edit_guests.py`, and
the `wedding.db` schema — it touches several places, so do it carefully.

The columns `form_current_city`, `form_hometown`, and `form_memory` still exist
but **this couple's form doesn't collect them**, so they stay empty and their
profile sections simply don't render. That's fine; leave them unless asked.

## Site edits: template, not generated output

`index.html` is **generated** — running `python3 build.py` overwrites it.

- HTML / CSS / inline JS → edit the `TEMPLATE` string in `build.py`.
- Per-guest / per-event data → lives in `wedding.db` (via `merge_guests.py` from
  the form, or hand-edits in `scripts/edit_guests.py`).
- Fragment builders (events, guide, guest JSON, etc.) → the `build_*` functions
  in `build.py`.

After any change: `python3 build.py`, confirm it landed in `index.html`, then
commit **both `build.py` and `index.html` together**. `wedding.db` is tracked —
commit it too whenever it changes.

## Content still to fill in (placeholders)

The template has clearly-marked blanks in square brackets, e.g.
`[Wedding location]`, `[Your welcome note goes here...]`, `[Cause name]`,
`[Your travel tip goes here.]`. These are the couple's own content to write.
**Fill them only with words Hilary or Elliot actually give you** — do not invent
their welcome note, registry causes, travel advice, or location. Ask them.

Also still couple-specific and worth reviewing: the "For a Good Time" tips, the
watercolor artwork and green/cream color scheme, the app icons, and the
date-based Travel↔Toasts tab swap (currently set to the old couple's date). The
overall visual design has NOT yet been decided for Hilary & Elliot.

## NEVER fabricate guest content (absolute rule)

Guest-profile fields — the "how you know us" story, karaoke answer, pronouns,
photo, memories, relationships, and any future per-guest field — may only contain
content that was **authored by the guest via the form**, or **authored by Hilary
or Elliot by hand** (via `scripts/edit_guests.py`). Do **not** generate, invent,
paraphrase, guess, or auto-fill any of it — not as a preview, not as demo data,
not "to see how it looks," not marked `[placeholder]`. An honest-empty profile is
always better than a fabricated one; these are real people who know each other.

Do not infer relationships from shared surnames, a shared "plus one" column,
shared addresses, or chains of reasoning. If it wasn't stated by the guest or by
Hilary/Elliot, leave it blank and ask.

*(Note: the memory/relationship `source` tags in the database currently still
accept the old couple's names. If Hilary & Elliot want to hand-author memories or
"Here with" couple-links, update those source tags to their names first — it
touches the `wedding.db` CHECK constraints, `scripts/edit_guests.py`, and the
validators in `build.py`. Flag this to them before doing it.)*

## Data pipeline

`merge_guests.py` reads the form responses (and optionally a guest-list export)
and writes `wedding.db`; then `build.py` regenerates `index.html`. Guest photos
submitted through the form are Google Drive links — fetch them with the **Google
Drive connector**, never `curl`/`wget`/`gdown` (private files fail). The core
build needs **only Python 3's standard library** — no pip packages. (Photo
face-cropping is the one optional exception and has its own setup script.)

## Typography & readability minimums

Read on phones, often outdoors, by a wide age range. **Body/description text:
14px minimum. Captions/metadata: never below 12px.** When tempted to shrink text
to fit, reflow the layout or trim the copy instead. (Gotcha: the minifier strips
`type="text"` from inputs, so CSS must also target `input:not([type])`.)

## Deploying

Push to `main` → Vercel auto-deploys in about a minute. There is no manual deploy
step. Commit `build.py` + `index.html` + `wedding.db` together so the live site
and the checked-in files stay in sync.
