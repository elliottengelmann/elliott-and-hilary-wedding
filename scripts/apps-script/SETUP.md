# Automatic guest-list updates — setup

When someone submits the wedding questionnaire, the site updates itself within
a couple of minutes. Nobody has to do anything.

You only have to set this up once, and it takes about ten minutes. There are
two halves: a **token** (so the sheet is allowed to talk to GitHub) and a
**script** (which does the talking). Everything else is already in the repo.

---

## How it works

```
Guest submits the form
  → Apps Script in the responses sheet wakes up
  → commits the new row + their photo to GitHub
  → GitHub Actions crops the photo and rebuilds the site
  → Vercel deploys it
```

The script runs **as the sheet's owner**, which is the whole reason this
design works: photos uploaded through a Google Form are private to the form's
owner. A build server couldn't fetch them without a separate Google service
account. Running inside the sheet sidesteps that completely.

---

## Part 1 — Make a GitHub token

A token is a long password that lets the script act on your GitHub account.
This one can only touch the wedding repo, and you can cancel it any time.

1. Go to <https://github.com/settings/personal-access-tokens/new>
2. **Token name:** `wedding-form-sync`
3. **Expiration:** pick a date after the wedding — 90 days is fine
4. **Repository access:** choose **Only select repositories**, then pick
   `elliott-and-hilary-wedding`
5. Under **Permissions → Repository permissions**, find **Contents** and set
   it to **Read and write**. Leave everything else alone.
6. Click **Generate token**, then **copy it**

> GitHub shows the token exactly once. Keep the tab open until Part 2 is done.
> If you lose it, delete that token and make a new one — no harm done.

---

## Part 2 — Install the script

1. Open **Fwedding Questionnaire (Responses)** in Google Sheets
2. **Extensions → Apps Script**
3. Delete whatever is in the editor, and paste in the entire contents of
   [`Code.gs`](Code.gs) from this folder
4. Click the **save** icon

### Add the token

5. In the left sidebar click the gear (**Project Settings**)
6. Scroll to **Script Properties** → **Add script property**
   - **Property:** `GITHUB_TOKEN`
   - **Value:** the token you copied
7. **Save script properties**

### Test it before turning it on

8. Back in the **Editor**, choose `syncNow` from the function dropdown at the
   top, and click **Run**
9. Google will ask for permission — it needs to read the sheet, read the
   photos in Drive, and reach github.com. Approve it.
   You'll likely hit a **"Google hasn't verified this app"** screen. That's
   normal for a script you wrote yourself: click **Advanced**, then
   **Go to (project name)**.
10. When it finishes, check
    <https://github.com/elliottengelmann/elliott-and-hilary-wedding/commits/main>
    — you should see a new commit called *"Form response from … (automated)"*

If that commit is there, everything works.

### Turn on the trigger

11. In the left sidebar click the **clock** (**Triggers**)
12. **Add Trigger**, and set:
    - Function to run: **`onFormSubmitHandler`**
    - Event source: **From spreadsheet**
    - Event type: **On form submit**
13. **Save**

Done. The next person to submit the form appears on the site on their own.

---

## Checking on it

- **Did the site update?**
  <https://github.com/elliottengelmann/elliott-and-hilary-wedding/actions> —
  each run shows a summary: how many guests, who's missing a photo, and which
  thumbnails were centre-cropped because no face was found.
- **Did the sheet fail to send?** In the Apps Script editor, the **Executions**
  tab (the ≡ icon) lists every run and any error. Google also emails the
  script's owner when a trigger fails.

## If something breaks

Nothing here is load-bearing for the site itself — it only automates what was
already being done by hand. To stop it, delete the trigger (step 11–13) and
things go back to manual.

| Symptom | Cause |
|---|---|
| `GITHUB_TOKEN script property is not set` | Part 1 step 6 was skipped or misspelled — it's case-sensitive |
| `GitHub 401` | Token expired or was revoked — make a new one and update the property |
| `GitHub 403` | Token lacks **Contents: read and write**, or the repo wasn't selected |
| Guest appears with initials instead of a photo | Their photo didn't upload; the Actions summary names them |
| Nothing happens on submit | Trigger missing — check the Triggers tab |

## A note on privacy

The repo is **public**, so the script deliberately **drops the email column**
before committing anything (`EXCLUDE_COLUMNS` at the top of `Code.gs`). Guest
emails never reach GitHub through this path. Don't remove that unless the
repo is private.
