# CLAUDE.md

Guidance for Claude Code when working in this repository.

This site is a fork of Elien Becque's `elienima.com` wedding PWA, adapted
for Elliott & Hilary. Elien is the operator (runs merges, builds, deploys,
CMS edits); Elliott & Hilary provide content and creative direction.

## NEVER fabricate guest profile content

This rule is absolute and applies to every Claude instance, every AI
assistant, and every human contributor working in this repo.

Guest profile fields — `how_we_know` (story), `least_favorite`, `city`,
`hometown`, memories, relationships, and **any future per-guest field** —
may only contain content that was:

1. **Authored by the guest themselves** via the Google Form (form CSVs
   merged into `wedding.db` by `merge_guests.py`), or
2. **Authored by Elien** (as operator), explicitly and by hand, via
   `scripts/edit_guests.py` or a direct edit to the DB.

**Do not generate, invent, paraphrase, guess, or auto-fill any of these
fields under any circumstance** — not as preview data, not as demo
content, not "to see how the design looks," not even prefixed with
`[Placeholder]` markers. No AI-written memories, no inferred hometowns,
no guessed cities, no assumed relationships.

If a field is empty and you need to demo something, pick a guest whose
data is genuinely populated or leave it empty and describe the behavior
in words. An honest-empty profile is always better than a fabricated one
— the app is read by real people who know these guests personally, and
fabricated content on a real profile is inappropriate and breaks trust.

Exception, narrow: relationship rows that are **directly stated** in a
form response or in something Elliott, Hilary, or Elien has said in
conversation. Transitive / chained inferences are NOT allowed even when
the chain looks airtight. Two people sharing a surname does not mean
they're spouses. When in doubt, leave it blank and ask.

## Curation lives in wedding.db

Hand-authored additions (location, hometown, memories, relationships)
live in three tables in `wedding.db`:

- `guest_locations (guest_key, current_city, hometown)`
- `guest_memories (guest_key, subject, text, source)`
- `relationships (guest_a_key, guest_b_key, label, source)`

These are the **single source of truth** for that data. They are
authored via `scripts/edit_guests.py` (preferred) or direct SQL.
`merge_guests.py` must never drop them — only the `guests` table is
rebuilt from CSV imports.

## Stay in Touch — `guest_contacts`

Post-wedding contact info (phone, email, Instagram, LinkedIn, Twitter/X,
BlueSky, Soundcloud) lives in a fourth table:

- `guest_contacts (guest_key, phone, email, instagram, linkedin,
  twitter, bluesky, soundcloud, source, submitted_at, updated_at)`

Two legitimate sources:

1. The **Contact & Socials Google Form** (merged in via
   `merge_guests.merge_contacts()`, tagged `source='form'`).
2. `scripts/edit_guests.py` when Elien hand-adds/corrects a row (tagged
   `source='elien'`).

The schema CHECK constraint is `source IN ('form', 'elien', 'nima')` —
`'nima'` is a legacy slot inherited from the source repo; new rows should
always tag `'elien'`. Form rows refresh on every merge run; rows tagged
`'elien'` are never overwritten by the form sync (that's the manual-
override mechanism). To release a manual override, clear every field in
the editor (deletes the row), then re-run the merge.

Guest-authored contact data is the **only** field set displaying data the
guest themselves wrote — and only because it came directly from them via
the Contact & Socials form. The "never fabricate" rule still applies:
never invent a phone/email/handle, never infer one from a shared
surname or address.

The rosa-rugosa bud badge on Los Invitados thumbnails and the "Stay in
Touch" profile section are both gated on `hasContacts === true`,
computed in `build.py` from a non-empty `guest_contacts` row.

## Seating chart also lives in wedding.db

The seating editor (`scripts/edit_seating.py`, port 8766) persists into:

- `seating_tags (tag_key, name, color, sort_order)`
- `seating_tables (day, table_key, table_num, name, capacity, sort_order, layout_json)`
- `seating_assignments (day, table_key, seat_idx, guest_id)`
- `seating_guest_meta (guest_id, tag_key, is_kid, note)`

Plus `seating_meta` for migration markers. `merge_guests.py` must never
drop these — they're independent of CSV import.

The seating editor reads guests from the `guests` table (filtered to
"attending ≥1 day or already seated") but does not write to it. RSVP
edits and add/delete-guest happen in `scripts/edit_guests.py` or via the
CSV merge.

## The "Here with" source rule

Constraint on the subset of relationship data that powers the "Here with"
section (romantic partners only — see `ROMANTIC_LABELS` in `build.py`):
**rows may only originate from Elien**, hand-authored via
`scripts/edit_guests.py` or by direct SQL. Tagged `source='elien'`.

**Every relationship row and every memory row must carry a `source`
field** whose value is one of `'elien'` or `'nima'` (schema-enforced).
On this site Elien is the sole operator, so `'elien'` in practice; the
`'nima'` slot is legacy from the source repo.

### Loopholes that are explicitly closed

Do not add a relationship or memory row from any of the following
sources, even if the case seems clear:

- A guest's form response implying a romantic pair ("I'm here with my
  partner Jane"). Guest-authored ≠ Elien-authored. Banned.
- Shared surname. Banned.
- Transitive chains of any depth. Banned.
- Shared address, group_house, or master-list row. Banned.
- "Preview" / "demo" / "sample" / "to see how the design reads". Banned.
- Pre-filling "for Elien to replace later." Banned.
- AI-suggested relationships or "it looks like these two are a couple"
  heuristics. Banned as concepts.
- Historical data carried in from old branches or prior commits without a
  proper source tag. Banned.

### When in doubt

Leave the field empty. Ask Elien. An empty "Here with" line is the
correct fallback — the section simply doesn't render when there are no
qualifying rows.

## Site edits: template, not generated output

`index.html` is **generated** by `build.py` from the `TEMPLATE` string
defined at the bottom of `build.py` (see `TEMPLATE = r"""..."""`),
combined with data from `wedding.db`. Running `python3 build.py`
overwrites `index.html`.

**All edits to the site's markup, styles, or scripts must be made in the
template, not in `index.html`.** Any change made directly to `index.html`
will be silently clobbered the next time `build.py` runs.

Concretely:
- HTML structure, CSS, inline JS → edit the `TEMPLATE` string in `build.py`.
- Per-guest / per-event data → edit the source CSVs (and/or
  `merge_guests.py` mappings), then rerun
  `python3 merge_guests.py && python3 build.py`.
- Fragment builders → edit the relevant `build_*` function in `build.py`.

After editing the template or any builder, regenerate:

```
python3 build.py
```

Then verify the change landed in `index.html` before committing. Commit
both `build.py` and the regenerated `index.html` together.

## `[[PLACEHOLDER]]` markers

This scaffold has ~24 `[[PLACEHOLDER]]` markers in `TEMPLATE` where
Elliott & Hilary's content needs to be inserted. Grep for `[[` in
`build.py` to find them. Categories:

- Venue / city references (`[[VENUE — …]]`)
- Long-form copy blocks (splash thank-you, gift intro, travel note)
- External URLs (group chat, shared photos album, travel/lodging sheets)
- Charity block names + descriptions (2 slots)
- Trip tips ("The Trip Is Best With:" — 3 slots)

Do NOT invent copy for these — wait for Elliott & Hilary to provide it.
The site can ship with placeholders visible; it's honest about being
mid-setup.

## `wedding.db` is tracked — commit it

`wedding.db` **is kept in the repository** and is the source of truth for
the guest/event data baked into `index.html`. Do not add it to
`.gitignore`. When `merge_guests.py` rewrites it (or when `build.py` is
run against an updated DB), commit the updated `wedding.db` alongside any
regenerated `index.html`.

CSV inputs (`export.csv`, `master_list.csv`, `form_responses.csv`,
`contact_form_responses.csv`) are gitignored on purpose — they're inputs
that get merged into `wedding.db`, and the DB is the artifact we ship.

## Downloading guest photos from Google Drive

Form-submitted guest photos are stored as
`https://drive.google.com/open?id=...` URLs in `form_responses.csv`.
**Always use the Google Drive MCP** (`get_file_metadata` then
`download_file_content`) — never `gdown`, `curl`, or `wget`. The MCP is
signed in as Elien; `gdown` and unauthenticated HTTP fail on non-world-
readable files.

Workflow:
1. Look up the file by Drive id (extracted from the form URL's `?id=`).
2. `get_file_metadata` to confirm mime type and the original filename
   (also how you spot uploads where the Drive filename and the form
   name disagree — flag those before saving).
3. `download_file_content`; the result is base64 in
   `content[0].embeddedResource.contents.blob`. Decode and write to
   `images/guests/<first>_<last>.<ext>`, matching the file's actual
   magic bytes.
4. Re-run `python3 merge_guests.py && python3 build.py` so each guest's
   `photo_url` flips from the Drive URL to the local path.

If `get_file_metadata` returns "Requested entity was not found", the
file is in a Drive account Elien doesn't have access to. List those
guests for Elien to escalate; do not invent a workaround.

## Typography & readability minimums

The app is read on phones, often outdoors, by people 25–80. Body text
below 14px starts to fail on iOS.

**Hard floors:**

- **Body / description text** (place notes, FAQ answers, event details,
  airport descriptions, any paragraph the reader is expected to *read*)
  — **14px minimum**.
- **Italic body text** — same 14px floor. Italic in Bodoni Moda already
  reads ~1px smaller.
- **Captions / metadata** (timestamps, secondary labels) — 13px OK;
  never below 12px.
- **Uppercase tracked labels** — 12px minimum. Letter-spacing already
  shrinks the optical size.

**Display sizes (no upper limit):**

- Script headings (Mea Culpa) — 22–32px depending on context.
- Section titles in serif — 16–22px.

**Practical rules of thumb:**

1. If it's a paragraph, set 14px. If it wraps to 3+ lines on a 375px
   viewport, set 14px and bump line-height to 1.6.
2. If you reach for `font-size: 10px` or `11px`, stop. Either it's a
   caption (12–13px) or it shouldn't be on the page.
3. The minifier strips redundant `type="text"` from `<input>`. CSS
   selectors targeting inputs must include `input:not([type])` alongside
   `input[type="text"]` or styling silently disappears in production.
4. Active state on the bottom tab bar uses `--accent-warm` (burnt
   orange) intentionally, not a darker green — sufficient contrast
   against the inactive `#8FB89E` sage.

When tempted to make text smaller "to fit", the answer is almost always
"reflow the layout" or "trim the copy", not "shrink the font".

## Other assets

- `sw.js`, `manifest.webmanifest`, `vercel.json`, `icons/`, `images/`
  are authored directly — no build step.
- Icons are composited from `images/rosa-rugosa.png` via
  `scripts/gen-icons.py` (Pillow). When Elliott & Hilary supply their
  motif, replace `rosa-rugosa.png` and re-run the script.
