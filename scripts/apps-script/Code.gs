/**
 * Fires when someone submits the wedding questionnaire, and commits the new
 * response to GitHub. That push starts .github/workflows/sync-guests.yml,
 * which rebuilds the site; Vercel deploys a minute or so later.
 *
 * This runs as the sheet's owner, which is why it can read the photo out of
 * Drive at all — form uploads are private to the form owner, so an outside
 * build server could not fetch them without separate Google credentials.
 * Running here sidesteps that entirely.
 *
 * Setup lives in SETUP.md next to this file.
 */

// ── Settings ────────────────────────────────────────────────────────────
var REPO = 'elliottengelmann/elliott-and-hilary-wedding';
var BRANCH = 'main';
var CSV_PATH = 'incoming/form_responses.csv';
var PHOTO_DIR = 'images/guests';

// Never commit this column to a public repo. See SETUP.md.
var EXCLUDE_COLUMNS = ['Email Address'];

// Column holding the Drive link to the guest's photo. Matched as a prefix so
// the question's exact wording can drift without breaking this.
var PHOTO_COLUMN_PREFIX = 'Share a photo';

var FIRST_NAME_COLUMN = 'First Name';
var LAST_NAME_COLUMN = 'Last Name';

// Photos run to several megabytes and Apps Script stops a run at six
// minutes, so backfillMissingPhotos() works through them a few at a time.
var BACKFILL_PER_RUN = 4;

// ── Entry point ─────────────────────────────────────────────────────────

/** Installable trigger target: runs on every form submission. */
function onFormSubmitHandler(e) {
  var lock = LockService.getScriptLock();
  // Two guests submitting at once would otherwise build commits from the
  // same parent and one would clobber the other.
  lock.waitLock(120 * 1000);
  try {
    syncToGitHub_();
  } finally {
    lock.releaseLock();
  }
}

/**
 * Manual run: commits the whole sheet as CSV, plus the newest row's photo.
 * Safe to run any time — use it to test the setup.
 */
function syncNow() {
  syncToGitHub_();
}

/**
 * Repair tool. Uploads photos for guests who don't have one in the repo yet,
 * which happens if Drive hiccuped during their submission — the CSV lands,
 * the photo doesn't, and they sit on an initials avatar with nothing
 * retrying. The Actions job summary names anyone in that state.
 *
 * Apps Script kills a run at six minutes and these files are megabytes, so
 * this does a few per run. Keep running it until it says nothing is missing.
 */
function backfillMissingPhotos() {
  var lock = LockService.getScriptLock();
  lock.waitLock(120 * 1000);
  try {
    var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheets()[0];
    var values = sheet.getDataRange().getValues();
    var header = values[0].map(function (h) { return String(h).trim(); });

    var present = listRepoPhotoStems_();
    var files = [];
    for (var r = 1; r < values.length && files.length < BACKFILL_PER_RUN; r++) {
      var photo = photoBlobFor_(header, values[r]);
      if (!photo || present[photo.stem]) continue;
      files.push({
        path: PHOTO_DIR + '/' + photo.name,
        content: Utilities.base64Encode(photo.bytes)
      });
      Logger.log('backfilling ' + photo.name);
    }

    if (!files.length) {
      Logger.log('nothing missing — every guest with a photo has one in the repo');
      return;
    }
    commitFiles_(files, 'Backfill ' + files.length + ' guest photo(s) (automated)');
    Logger.log('run again if more are still missing');
  } finally {
    lock.releaseLock();
  }
}

/** Stems of every photo already committed, as a lookup. */
function listRepoPhotoStems_() {
  var api = 'https://api.github.com/repos/' + REPO;
  var ref = ghJson_(api + '/git/ref/heads/' + BRANCH);
  var commit = ghJson_(api + '/git/commits/' + ref.object.sha);
  var tree = ghJson_(api + '/git/trees/' + commit.tree.sha + '?recursive=1');

  var out = {};
  (tree.tree || []).forEach(function (node) {
    if (node.type !== 'blob') return;
    if (node.path.indexOf(PHOTO_DIR + '/') !== 0) return;
    var rest = node.path.substring(PHOTO_DIR.length + 1);
    if (rest.indexOf('/') > -1) return;  // skip derived/
    var dot = rest.lastIndexOf('.');
    out[dot > -1 ? rest.substring(0, dot) : rest] = true;
  });
  return out;
}

// ── Core ────────────────────────────────────────────────────────────────

function syncToGitHub_() {
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheets()[0];
  var values = sheet.getDataRange().getValues();
  if (values.length < 2) {
    Logger.log('sheet has no responses yet');
    return;
  }

  var header = values[0].map(function (h) { return String(h).trim(); });
  var keep = [];
  for (var i = 0; i < header.length; i++) {
    if (EXCLUDE_COLUMNS.indexOf(header[i]) === -1) keep.push(i);
  }

  var csv = values.map(function (row) {
    return keep.map(function (i) { return csvCell_(row[i]); }).join(',');
  }).join('\r\n') + '\r\n';

  // The photo of whoever submitted last. The CSV carries everyone, but only
  // the newest row can have a photo we haven't already committed.
  var last = values[values.length - 1];
  var photo = photoBlobFor_(header, last);

  var files = [{ path: CSV_PATH, content: Utilities.base64Encode(csv, Utilities.Charset.UTF_8) }];
  if (photo) {
    files.push({ path: PHOTO_DIR + '/' + photo.name, content: Utilities.base64Encode(photo.bytes) });
  }

  commitFiles_(files, photo
    ? 'Form response from ' + photo.stem + ' (automated)'
    : 'Form response (automated)');
}

/**
 * One CSV cell, quoted the way Python's csv module expects to read it.
 * Dates are rendered the way Google's own CSV export does, so the file
 * stays diff-friendly against a hand export.
 */
function csvCell_(value) {
  if (value === null || value === undefined) return '';
  var s;
  if (Object.prototype.toString.call(value) === '[object Date]') {
    s = Utilities.formatDate(value,
      SpreadsheetApp.getActiveSpreadsheet().getSpreadsheetTimeZone(),
      'M/d/yyyy H:mm:ss');
  } else {
    s = String(value);
  }
  if (s.indexOf('"') > -1 || s.indexOf(',') > -1 ||
      s.indexOf('\n') > -1 || s.indexOf('\r') > -1) {
    return '"' + s.replace(/"/g, '""') + '"';
  }
  return s;
}

/**
 * Resolve the photo for one response row.
 * Returns {name, stem, bytes} or null when the guest uploaded nothing.
 */
function photoBlobFor_(header, row) {
  var photoIdx = -1, firstIdx = -1, lastIdx = -1;
  for (var i = 0; i < header.length; i++) {
    if (header[i].indexOf(PHOTO_COLUMN_PREFIX) === 0) photoIdx = i;
    if (header[i] === FIRST_NAME_COLUMN) firstIdx = i;
    if (header[i] === LAST_NAME_COLUMN) lastIdx = i;
  }
  if (photoIdx === -1 || firstIdx === -1 || lastIdx === -1) {
    Logger.log('could not find photo/name columns — check the header row');
    return null;
  }

  var url = String(row[photoIdx] || '').trim();
  if (!url) return null;

  var m = url.match(/[?&]id=([A-Za-z0-9_-]+)/) || url.match(/\/d\/([A-Za-z0-9_-]+)/);
  if (!m) {
    Logger.log('unrecognised Drive link: ' + url);
    return null;
  }

  var file;
  try {
    file = DriveApp.getFileById(m[1]);
  } catch (err) {
    Logger.log('cannot open Drive file ' + m[1] + ': ' + err);
    return null;
  }

  // Must match resolve_photo() in merge_guests.py exactly, or the merge
  // won't find the file and the guest gets an initials avatar.
  var stem = (String(row[firstIdx]).trim() + '_' + String(row[lastIdx]).trim())
    .toLowerCase().replace(/ /g, '_');

  return {
    name: stem + extensionFor_(file),
    stem: stem,
    bytes: file.getBlob().getBytes()
  };
}

/** Pick a file extension. normalize_photos.py converts anything exotic. */
function extensionFor_(file) {
  var byMime = {
    'image/jpeg': '.jpeg',
    'image/png': '.png',
    'image/heic': '.heic',
    'image/heif': '.heif',
    'image/webp': '.webp',
    'image/gif': '.gif'
  };
  var ext = byMime[file.getMimeType()];
  if (ext) return ext;
  var dot = file.getName().lastIndexOf('.');
  return dot > -1 ? file.getName().substring(dot).toLowerCase() : '.jpeg';
}

// ── GitHub ──────────────────────────────────────────────────────────────

/**
 * Commit several files as ONE commit via the Git Data API.
 *
 * The simpler Contents API writes one file per call, which would mean two
 * commits, two workflow runs, and a window where the CSV names a guest whose
 * photo isn't there yet. It also puts the path in the URL, which the curly
 * quotes and accents in some guests' names make needlessly fragile.
 */
function commitFiles_(files, message) {
  var api = 'https://api.github.com/repos/' + REPO;

  var ref = ghJson_(api + '/git/ref/heads/' + BRANCH);
  var baseCommitSha = ref.object.sha;
  var baseCommit = ghJson_(api + '/git/commits/' + baseCommitSha);

  var tree = files.map(function (f) {
    var blob = ghJson_(api + '/git/blobs', 'post', {
      content: f.content,
      encoding: 'base64'
    });
    return { path: f.path, mode: '100644', type: 'blob', sha: blob.sha };
  });

  var newTree = ghJson_(api + '/git/trees', 'post', {
    base_tree: baseCommit.tree.sha,
    tree: tree
  });

  var commit = ghJson_(api + '/git/commits', 'post', {
    message: message,
    tree: newTree.sha,
    parents: [baseCommitSha]
  });

  ghJson_(api + '/git/refs/heads/' + BRANCH, 'patch', { sha: commit.sha });
  Logger.log('committed ' + commit.sha.substring(0, 7) + ': ' + message);
}

function ghJson_(url, method, payload) {
  var token = PropertiesService.getScriptProperties().getProperty('GITHUB_TOKEN');
  if (!token) throw new Error('GITHUB_TOKEN script property is not set — see SETUP.md');

  var options = {
    method: method || 'get',
    headers: {
      Authorization: 'Bearer ' + token,
      Accept: 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28'
    },
    muteHttpExceptions: true
  };
  if (payload) {
    options.contentType = 'application/json';
    options.payload = JSON.stringify(payload);
  }

  var res = UrlFetchApp.fetch(url, options);
  var code = res.getResponseCode();
  if (code < 200 || code >= 300) {
    throw new Error('GitHub ' + code + ' on ' + url + ': ' + res.getContentText().substring(0, 400));
  }
  return JSON.parse(res.getContentText());
}
