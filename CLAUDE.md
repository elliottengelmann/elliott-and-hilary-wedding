# CLAUDE.md — Hilary & Elliott's Wedding App

Guidance for Claude Code working in this repository. Read this fully before
making changes. This project was adapted from a template built for another
wedding, so watch for anything still worded for the old couple.

## What this is

A static, installable Progressive Web App (PWA) for **Hilary & Elliott's**
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
- **No RSVPs.** No attendance tracking, no RSVP-gated schedule. Every guest sees
  the full schedule. The guest directory is driven purely by the form:
  **anyone who fills out the Google Form gets a profile.** Note: the guest
  directory screen is titled "Who's Coming" in the UI (renamed from "Los
  Invitados" at the couple's request) — that's just a label, not attendance
  tracking; it lists everyone who filled out the form, regardless of whether
  they're actually coming.

## The guest form and how fields map

Guests fill a Google Form. Its columns map into the app like this (see
`merge_guests.py`, function that reads the form):

| Form question | App field | Shown as |
|---|---|---|
| First Name / Last Name | `first_name` / `last_name` | the guest's name |
| Pronouns | `pronouns` | small line under the name |
| How do you know the couple? | `how_we_know` | "How I know Hilary and Elliott" |
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

Most of the couple's content (welcome note, schedule, FAQs, local guide,
travel/lodging, registry, links) has been filled in from the couple's own
Google Doc. Still open: the Zola registry link is wired up; the "Get to Know
You" form question spec is pending a follow-up from the couple; the
post-wedding "stay in touch" form is deferred (their call, TBD). Any new
bracketed blank you find, e.g. `[Cause name]`, is the couple's own content to
write — **fill it only with words Hilary or Elliott actually give you.**

**Visual design:** a simple coastal-Maine palette is applied — navy `#1F6E8C`
(primary), warm cream `#F5F0EB` (background), lobster-red `#E24A2E` (accent).
Tweak freely. Note: the CSS variables are still named `--primary-green` /
`--accent-warm` etc. (legacy names, coastal values) — rename them if you tidy up.

**Motif — three separate pieces, don't conflate them:**

- **Splash screen + app icons + small accent glyphs:** the couple's own
  hand-drawn "two lobsters forming a heart" illustration
  (`images/lobsters.jpg` — the raw file they uploaded; don't overwrite it).
  A cropped/background-removed transparent derivative lives at
  `images/lobsters.png` and is what the splash screen actually references
  (`<img>` in the `.splash-image` div). The app icons
  (`icons/icon.svg`, `icon-192.png`, `icon-512.png`,
  `icon-maskable-512.png`, `apple-touch-icon.png`) are generated from the
  same artwork composited onto the cream (`#F5F0EB`) background, matching
  the site's existing icon style (rounded square for "any"/apple-touch,
  edge-to-edge for maskable — safe-zone content stays within the center
  ~50% for the maskable variant). The two small accent glyphs (the
  PWA-update badge and the tiny bud icon next to a guest's "Stay in Touch"
  label) are hand-coded inline SVGs redrawn as mini lobster-claw/heart
  shapes to match — not derived from the image file. If the couple wants
  to swap in different artwork, regenerate all of these together (there's
  no script for it — it was done by hand with `sips` for cropping and the
  `scripts/remove-paper-bg.mjs` + `sharp` for background removal/icon
  compositing; see git history on this file for the exact commands).
- **Full-page background:** `.app-container`'s `background` is a Popham
  Beach watercolor (`images/popham.png`), washed out under a ~0.88-opacity
  cream tint (a two-layer CSS `background`: gradient over the image) so text
  laid directly on it stays legible. `images/nasturtiums.png` and
  `images/rosa-rugosa.png` are still real assets in the repo, unused for
  the background — the guest-profile "shared contacts" thumb badge still
  uses rosa-rugosa.png.

## NEVER fabricate guest content (absolute rule)

Guest-profile fields — the "how you know us" story, karaoke answer, pronouns,
photo, memories, relationships, and any future per-guest field — may only contain
content that was **authored by the guest via the form**, or **authored by Hilary
or Elliott by hand** (via `scripts/edit_guests.py`). Do **not** generate, invent,
paraphrase, guess, or auto-fill any of it — not as a preview, not as demo data,
not "to see how it looks," not marked `[placeholder]`. An honest-empty profile is
always better than a fabricated one; these are real people who know each other.

Do not infer relationships from shared surnames, a shared "plus one" column,
shared addresses, or chains of reasoning. If it wasn't stated by the guest or by
Hilary/Elliott, leave it blank and ask. (This is about not *guessing* — using a
plus-one list Hilary/Elliott hand you directly, as the stated source, is fine;
that's authored by them, not inferred.)

*(The memory/relationship `source` tags now accept `'elliott'` / `'hilary'` —
renamed from the old couple's `'elien'` / `'nima'` across the `wedding.db` CHECK
constraints, `scripts/edit_guests.py`, and the validators in `build.py`, so
Hilary & Elliott can hand-author memories and "Here with" couple-links.)*

**"Here with" plus-ones who never filled the guest form:** a `relationships`
row can point at someone with no `guests` row at all (they never submitted the
small form) — `guest_a_name`/`guest_b_name` hold a fallback display name for
that side. `build_herewith_for()` in `build.py` renders them as plain text
(not a clickable chip, since there's no profile/stub to open) instead of
silently dropping them. If that person later fills out the form themselves,
their real profile takes over automatically on the next sync — no relationship
data needs to change. Imported from "Wedding Guest List - FINAL" (the
couple's Google Sheet of every guest + their named plus-one) on 2026-09-10;
3 pairs were skipped because the two directions of the same pair disagreed on
spelling (Gabe Nicholas/Nicolas, Andrew Garsetti/Garcetti, Allie/Annie
Goodman) — ask Hilary/Elliott for the correct spelling before adding those.

## Data pipeline

`merge_guests.py` reads `form_responses.csv` — a CSV export of the Google
Form's linked response sheet — and writes the `guests` table in `wedding.db`;
then `build.py` regenerates `index.html`. There is no separate RSVP/guest-list
export to reconcile against: the form is the *only* source, matching the
"anyone who fills out the form gets a profile" rule above. To sync: pull the
latest rows from the linked Google Sheet via the **Google Drive connector**,
write them out as `form_responses.csv` (gitignored, transient — regenerate
each time, don't commit it), run `python3 merge_guests.py`, then
`python3 build.py`. Guest photos submitted through the form are Google Drive
links — fetch them with the Drive connector too, never `curl`/`wget`/`gdown`
(private files fail); until a photo is pulled down into `images/guests/`, the
guest's profile keeps the raw (non-rendering) Drive share link. The core build
needs **only Python 3's standard library** — no pip packages. (Photo
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
