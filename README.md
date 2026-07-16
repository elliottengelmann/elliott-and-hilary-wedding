# Elliott & Hilary — Wedding PWA

A static Progressive Web App for Elliott & Hilary's wedding. Scaffolded from
Elien Becque's `elienima.com` starter kit; ~all Elien/Nima-specific content
has been replaced with `[[PLACEHOLDER]]` markers.

## Project layout

```
build.py                Generator — reads wedding.db, writes index.html
merge_guests.py         Ingest — merges Google Form CSVs → wedding.db
wedding.db              SQLite source of truth (empty schema on first run)
index.html              Rendered app shell (auto-generated; do NOT edit)
manifest.webmanifest    PWA manifest
sw.js                   Service worker (cache-first shell + SWR for fonts)
vercel.json             Static hosting + headers (SW, manifest, icons)
icons/                  PWA icons (regenerate via scripts/gen-icons.py)
images/                 Motif artwork (rosa-rugosa is a scaffold placeholder)
scripts/
  edit_guests.py        Local CMS (localhost:8765) for guests / relationships /
                        memories / contacts / guide places / cafes
  edit_seating.py       Local CMS (localhost:8766) for seating chart
  gen-icons.py          Regenerate the PWA icon family from images/rosa-rugosa.png
  process_guest_images.py  Generate face-cropped thumbnails from guest photos
```

## Setup checklist

Before this scaffold becomes a real site, someone needs to provide:

1. **Content packet from Elliott & Hilary** — names, wedding date + timezone,
   venue city, story, FAQs, gift/registry, aesthetic direction.
2. **Motif artwork** — swap `images/rosa-rugosa.png` for their chosen motif,
   then `python3 scripts/gen-icons.py` to regenerate icons.
3. **Google Forms** — an RSVP form and a Contact & Socials form in their
   Google account. Column structure must match what `merge_guests.py`
   expects (or update the mappings there).
4. **Placeholder pass** — grep for `[[` in `build.py`'s `TEMPLATE` string and
   replace each marker with real copy.
5. **Wedding date** — set `TOASTS_CUTOVER` in the TEMPLATE (currently a
   stub Jan 1 2099) to the arrival date, so the Toasts tab activates on time.

## Local development

Everything renders from `wedding.db`. Two steps to see the site locally:

```
python3 build.py
npx --yes serve .           # or: python3 -m http.server 5173
```

Open the localhost URL on a phone on the same wifi to test PWA install.

## Deploying to Vercel

Vercel is wired to this GitHub repo. Pushing to `main` auto-deploys.
`vercel.json` sets Service-Worker-Allowed on `/sw.js` and long-caches
`/icons/*`.

## Editing guest data

Everything guest-related happens through the local CMS:

```
python3 scripts/edit_guests.py
# open http://localhost:8765
```

Add guests, tag relationships, write memories, curate current cities. All
writes go into `wedding.db`. After a session:

```
python3 build.py
git add wedding.db index.html sw.js manifest.webmanifest
git commit -m "Content update"
git push
```

## Seating chart

Separate CMS for the seating chart:

```
python3 scripts/edit_seating.py
# open http://localhost:8766/seating
```

Auto-saves to `wedding.db` after every edit.

## Guest photos

Source photos live at `images/guests/<key>.{jpg,png}`. Generate compressed
face-cropped derivatives before deploying:

```
bash scripts/setup-photos-venv.sh          # one-time (dlib compile takes ~5 min)
.venv/bin/python scripts/process_guest_images.py
python3 build.py
```

Both sources and derivatives get committed — no build step on Vercel.
