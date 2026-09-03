# Getting Started — Hilary & Elliot's Wedding App

Welcome! This is your wedding app: a phone-installable website with a guest
"face book," a schedule, a local guide, a registry, and more. This guide gets
you from zero to editing it yourself. **You do not need to know how to code** —
you'll do almost everything by *asking Claude Code in plain English*.

A few words you'll see (in plain terms):
- **Repo / repository** — the folder that holds all the app's files, stored on
  GitHub (a website that keeps the master copy safe).
- **Clone** — make a copy of that folder onto your own Mac so you can work on it.
- **Claude Code** — the assistant you type to in the Terminal; it does the
  technical work for you.
- **Deploy** — push a change live so real people see it. Yours happens
  automatically (see the bottom).

---

## One-time setup (do this once, on the Mac that will edit the site)

### 1. Install the developer tools (gives you Git *and* Python in one step)

Git (the tool that copies code down and back up) and Python (what builds the
site) both come from Apple's free "Command Line Tools."

1. Open the **Terminal** app (press `Cmd-Space`, type "Terminal", hit Return).
2. Paste this and press Return:
   ```
   xcode-select --install
   ```
3. A little window pops up — click **Install**, then **Agree**. Wait for it to
   finish (a few minutes).

**Expect:** when it's done, typing `git --version` and then `python3 --version`
in the Terminal each print a version number instead of an error.

### 2. Get the code onto your Mac ("clone the repo")

1. In your web browser, open your repo page:
   `https://github.com/<your-username>/elliott-and-hilary-wedding`
2. Click the green **`< > Code`** button, make sure **HTTPS** is selected, and
   click the copy icon to copy the web address.
3. Back in Terminal — **you can use whatever Terminal tab you already have
   open** — paste these two lines one at a time (replace the pasted-address part
   with what you copied):
   ```
   cd ~/code 2>/dev/null || mkdir -p ~/code && cd ~/code
   git clone <paste-the-address-you-copied>
   ```
   The first line makes/enters a folder called `code` in your home folder; the
   second copies the app into it.

**Expect:** a new folder `~/code/elliott-and-hilary-wedding` now exists with all
the files in it. (If GitHub asks you to sign in, do so with your GitHub account.)

### 3. Open Claude Code inside the app folder

In the same Terminal tab:
```
cd ~/code/elliott-and-hilary-wedding
claude
```
**Expect:** Claude Code starts up and its prompt appears. It's now "standing
inside" your app and can read every file here — including this guide and the
`CLAUDE.md` file, which teaches it everything about your project.

### 4. Connect Google Drive (so Claude can pull form responses + photos)

Your guests' form answers and photos live in Google Drive. This lets Claude Code
read them for you.

1. Open **claude.ai** in your browser, signed in with the **same Google account**
   that owns the wedding Google Form and Drive.
2. Click your **profile icon** (top right) → **Settings** → **Connectors**.
3. Find **Google Drive**, turn it **on**, click **Sign in with Google**, pick
   your account, and approve access.

**Expect:** Google Drive shows as connected. To confirm it works, go back to
Claude Code and ask: *"Read the header row of my guest form sheet at
&lt;paste the Google Sheet link&gt;."* If it can read the column names, you're set.

*(Note: connectors require a paid Claude plan. If Google Drive doesn't appear,
that's usually why.)*

---

## Your first task once you're set up: add the lobster art 🦞

The whole site is designed around a hand-drawn lobster illustration (two
lobsters forming a heart). The design already points to it everywhere — but the
image file itself isn't in the repo yet, so that spot is currently blank. To
finish it:

1. Get the lobster image (Elien/Hilary has it) and save it as a file named
   **exactly** `lobster.png`.
2. Put it in the app's `images` folder. In **Finder**: menu **Go → Go to
   Folder…**, paste this and press Return, then drag `lobster.png` in:
   ```
   ~/code/elliott-and-hilary-wedding/images
   ```
3. Open Claude Code in the app folder and say: **"I added the lobster image —
   build and publish it."** It rebuilds and pushes, and the lobster appears on
   the live site in about a minute.

*Optional polish for later:* the image has a white background, so it shows a
faint white box on the cream screens. Ask Claude Code to make the background
transparent whenever you want it cleaner (there's a helper for it in `scripts/`).

---

## The one thing you'll actually do over and over

**When guests fill out the form and you want the site updated**, open Claude Code
in the app folder (step 3) and just say:

> "Sync the new guest form responses and update the site."

Claude Code will pull the latest responses (and photos), rebuild the site, and
push it. **Vercel** (your hosting) then puts it live automatically in about a
minute. That's it.

**To change wording or content** (the welcome note, schedule, registry, FAQs,
your wedding location, etc.), just describe what you want:

> "Set our wedding location to Ojai, California, and write the welcome note as: …"

The app currently has clearly-marked blanks in square brackets like
`[Wedding location]` and `[Your welcome note goes here...]` — tell Claude Code
what to put in them.

---

## Good to know

- **The site auto-updates.** Every change that gets pushed to GitHub goes live on
  its own through Vercel — you don't "deploy" by hand.
- **Ask Claude Code first, always.** It knows this project (via `CLAUDE.md`). If
  you're unsure how to do something, describe the goal in plain English and let
  it handle the how.
- **Photos are a slightly more advanced step** — when you're ready to add guest
  photos, ask Claude Code to walk you through it; it may need a one-time extra
  install.
- **You can't easily break anything permanently.** The master copy is safe on
  GitHub, and old versions can always be brought back. Experiment.
