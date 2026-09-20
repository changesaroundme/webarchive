# webarchive

Archive civic webpages as single PDFs with change tracking — the automated
alternative to the Safari bookmarklets (see "PublicInput PDF Bookmarklet" and
"AustinTexas.gov PDF Bookmarklet" in Archive - Changes Around Me/Tooling).

- **PublicInput (Speak Up Austin) project pages** → one content-sized PDF page per tab.
- **PublicInput survey pages** (a "Page 1..N" workflow gated behind required questions,
  e.g. publicinput.com/x30854) → one PDF page per survey page. Nothing is answered or
  submitted: the gate is client-side only, so each step is fetched directly from the
  server and captured blank, with Continue/Back buttons hidden.
- **ArcGIS StoryMaps** are captured via the story's own `/print` rendition (full
  content in document order — the interactive scrollytelling view prints scrambled);
  map canvases are screenshotted like iframes. The capture is still filed under the
  story's normal URL.
- **Any other page** (austintexas.gov project pages, etc.) → one
  content-sized PDF page: the page is scrolled through so lazy images load, collapsed
  sections (Drupal accordions, Bootstrap collapse, ARIA accordions like TxDOT's
  one-panel-at-a-time "melodeon", `<details>`) are expanded, overlays are hidden, fixed
  headers pinned into the flow, and viewport-sized sections (StoryMaps' 100vh covers)
  frozen at their on-screen height so they can't re-inflate to the full page height
  during printing.

The script decides which kind it's looking at by itself; the command is the same.

## One-time setup

The daily job runs in a container (see **Install and uninstall** below — that section is
the complete list of what touches the Mac and how to take it out again). Running the
script directly on the Mac is still possible, for one-off captures or debugging:

    cd webarchive
    python3 -m venv .venv
    .venv/bin/pip install playwright pikepdf boto3
    .venv/bin/playwright install chromium

The venv lives inside the checkout and is gitignored; delete the folder to remove it.

## Usage

    .venv/bin/python archive_page.py https://www.speakupaustin.org/centralcity
    .venv/bin/python archive_page.py <url> "My Name.pdf"      # explicit output path
    .venv/bin/python archive_page.py <url> --out-dir=~/some/dir
    .venv/bin/python archive_page.py <url> --original-hero     # keep the hero's source PNG (+~7 MB)
    .venv/bin/python archive_page.py <url> --force             # export even if nothing changed
    .venv/bin/python archive_page.py <url> --width=1440
    .venv/bin/python archive_page.py <url> --no-docs           # skip auto-download of linked documents
    .venv/bin/python archive_page.py <url> --all-docs          # lift the 500 MB per-page document cap
    .venv/bin/python archive_page.py --all --verify            # re-download and hash every linked document
    .venv/bin/python archive_page.py --all                    # re-check every archive page in sources.csv; writes only what changed
    .venv/bin/python archive_page.py --due                    # only the pages whose check interval has elapsed (the daily job)
    .venv/bin/python archive_page.py --all --verbose          # batch with full diffs and every unchanged file listed
    .venv/bin/python archive_page.py --fetch "Central City District Plan" 'https://…/report.pdf' 'https://…/boards.pdf'
                                                              # save linked files into that page's folder

Every capture lands in `Archive - Changes Around Me/Tooling/Web Archive/<page title>/`,
named `<page title> - <2026-08-30 1742>.pdf` (export date + time) — one flat archive,
whatever the site. The owning organization (PublicInput customer id → `PUBLICINPUT_ORGS`
at the top of the script; hostname for other sites) and the source URL are recorded in each
capture's metadata; organizing happens in the knowledge base, not in the folders.
If two different pages ever share a title, the change check refuses to use the other
page's capture as a baseline and says so.

## The page list and the daily job

`--all` and `--due` read `../calendars/sources.csv` (the calendars repo checked out next to
this one; `--registry=FILE` to point elsewhere): every row with `archive: yes` and
`status: active`. The registry only supplies the list — captures still land in the page-title
folder. `--due` skips a page until its `check` interval has elapsed (`daily`, `weekly`,
`every 3 days`, …) since the last check; while a row is inside its `expect` window
(`every 1 year Jun-Aug` for the UTP page) the interval tightens to daily. A page not in
the registry is no longer visited: add a row, or leave its folder as history.

Both modes write `../calendars/docs/captures.json` — per slug, when the page was last
checked and the file name and time of its newest capture. Nothing else goes in it (no
paths), because it is published: the calendars build reads it to fill the *Archive* column
of the public sources table, so it rides along with the next `git push` of the calendars repo.

Batch output is compact — one line per tab (`+3 / -1 lines`), one per saved or failed
document, a count of unchanged ones — because the full diff is already inside each PDF as
`changes.diff`; `--verbose` prints it to the log as a single-page run does.

## Running it in a container (the daily job)

Nothing about the job runs natively on the Mac. `Dockerfile` builds an image with
Python, Playwright's Chromium and pikepdf; `run.sh` runs the script inside it with
Apple's container runtime (macOS 26, [`container`](https://github.com/apple/container)),
bind-mounting this checkout at `/app` (read-only — edits to the script take effect on
the next run, no rebuild), the vault's `Web Archive` folder at `/archive` and the
calendars checkout at `/calendars`. Any arguments pass straight through:

    ./run.sh --due
    ./run.sh --all --verbose
    ./run.sh https://www.speakupaustin.org/centralcity --force

## Install and uninstall

Everything the job puts on the Mac, in the order it goes on, and how each piece comes
off. The principle: the only things running natively are a one-line launchd trigger and
Apple's container runtime; Chromium, Python and the script run inside the container.

### Install

1. **Apple's container runtime** (macOS 26, Apple silicon):

       brew install container

   Do **not** run `brew services start container` — that would keep the service resident
   as a login item. `run.sh` starts it for the duration of a run and stops it afterwards.

2. **The Linux kernel the containers boot** — one time, and the kernel command needs the
   service up, so:

       container system start --disable-kernel-install
       container system kernel set --recommended
       container system stop

3. **Pin the archive folder locally.** In Finder, right-click
   `Archive - Changes Around Me/Tooling/Web Archive` → **Keep Downloaded**. iCloud
   otherwise evicts old captures to stubs the container cannot read, and the change check
   reads every page's previous capture.

4. **First run, from a terminal** (builds the image, ~90 s, then a full pass):

       cd webarchive && ./run.sh --all

   The first time, macOS asks whether `container-runtime-linux` may access files managed
   by iCloud Drive — that is the runtime serving the archive folder into the VM. Allow.

5. **Certificates a host fails to send** (needed for `ftp.txdot.gov`; see that section):

       certs/fetch-chain.sh ftp.txdot.gov

   Commit `certs/extra-ca.pem`.

6. **Object storage** (optional; see that section): `~/.config/cam-webarchive/r2.env`,
   `chmod 600`, then `./run.sh --sync --dry-run` and `./run.sh --sync`.

7. **The daily job:**

       ./install.sh
       ./install.sh status

   `install.sh` writes `~/Library/LaunchAgents/com.changesaroundme.webarchive.plist` with
   this checkout's path filled in and loads it: `./run.sh --due` daily at 6:30am, or at
   the next wake if the Mac was asleep. Output goes to `~/Library/Logs/cam-webarchive.log`.
   Re-running `./install.sh` after moving the checkout refreshes the paths.

### What is on the Mac afterwards

| Item | Where | Put there by |
|---|---|---|
| Apple's `container` CLI and runtime | Homebrew (`/opt/homebrew`) | `brew install container` |
| Linux kernel + the runtime's data (images, the build helper, container disks) | the runtime's data directory under `~/Library` | step 2 and the first `run.sh` |
| Files-and-Folders permission for `container-runtime-linux` → iCloud Drive | System Settings → Privacy & Security → Files and Folders | the prompt on the first run |
| Keep Downloaded on `Web Archive` | iCloud Drive setting on that folder | you, in Finder |
| `com.changesaroundme.webarchive.plist` | `~/Library/LaunchAgents/` | `install.sh` |
| `cam-webarchive.log` | `~/Library/Logs/` | the job |
| `r2.env` (R2 credentials) | `~/.config/cam-webarchive/` | you |
| `.venv/`, `certs/extra-ca.pem`, `__pycache__/` | this checkout | you / the script |

Nothing goes in `/usr/local`, no cron entry, no system Python packages, no Chromium on
macOS.

### Uninstall

In order; each step is independent, so stop wherever you like.

1. **The job** — unloads and deletes the plist and the log, deletes the image and the
   build helper, stops the container service:

       ./install.sh remove

   `launchctl list | grep changesaroundme` should then print nothing.

2. **R2 credentials** (if you set them up):

       rm -r ~/.config/cam-webarchive

   The bucket and its API token live in Cloudflare and are removed there if you want
   them gone; the sync never deletes objects, so the bucket keeps every version until
   you delete the bucket.

3. **The iCloud Drive permission:** System Settings → Privacy & Security → Files and
   Folders → `container-runtime-linux` → turn off iCloud Drive (or remove the entry).
   Harmless to leave if you keep the runtime for other containers.

4. **Keep Downloaded:** right-click the `Web Archive` folder in Finder → Remove Download,
   if you want iCloud to manage it again.

5. **Apple's container runtime**, if nothing else uses it:

       container system stop
       brew uninstall container

   `brew uninstall` removes the CLI and runtime; the kernel and any leftover images live in
   the runtime's data directory under `~/Library` — `container system df` shows what is
   there before you uninstall, and `brew uninstall --zap container` removes it too.

6. **The checkout** — `rm -rf webarchive` removes the venv, the pinned certificate and
   the scripts. The archive itself (`Tooling/Web Archive` in the vault) is untouched by
   any of this.

### Fonts

The image installs Lato, Liberation Sans and Noto and maps the fonts the civic sites
ask for (Avenir Next, Helvetica Neue, Arial, Segoe UI) onto them in
`container/fonts.conf`. Without that, Chromium falls back to the much wider DejaVu Sans
and headings wrap onto extra lines — on Experience Builder pages they then overlap the
fixed-height block below. A new alias needs an image rebuild (`./install.sh remove`,
then the next run builds).

### Known quirk

If you resize or scroll the terminal while `./run.sh` is running, Apple's `container`
prints `failed to send signal: ["signal": 28 …` once per event. Signal 28 is the
terminal-resize signal; the runtime does not forward it, nothing in the run is affected,
and the daily job (no terminal) never sees it.

## Linked pages kept as PDFs

Some links are pages rather than files but belong with a page's documents: PublicInput
email newsletters (`publicinput.com/Email/<id>`, linked as "Project Update: March 2025").
Each is printed once into `Attachments/` as `<email title>.pdf` (the email body itself,
from `/EmailHtml/<id>`), with its text embedded like any capture, and remembered in
`.index.json` so it is never fetched twice — an email does not change. Other kinds of
linked page can join `LINKED_PAGE_RE` the same way.

## Certificates a host fails to send

`ftp.txdot.gov` serves its own certificate without the intermediate that links it to a
root, so every download from it fails with "unable to verify the first certificate"
(browsers fetch the missing link themselves; Node, which Playwright's download client
runs on, does not). `certs/fetch-chain.sh ftp.txdot.gov` walks the certificate's
issuer links and appends the intermediates to `certs/extra-ca.pem`, which the script
hands to Node on every run. Roots are never pinned. Re-run it for any host that shows
that error; the file is committed so the container and any other runner get it too.

## Object storage (Cloudflare R2)

`r2sync.py` mirrors the archive into an S3-compatible bucket after every batch run, and
on its own with `./run.sh --sync` (`--dry-run` lists what would upload without uploading;
`--verify` also compares the sizes of what is already there). The bucket is append-only
history: each capture and each document
version is uploaded once under a key that never changes, and nothing is ever deleted
or overwritten. Keys:

    <slug>/<YYYY-MM-DD HHMM> - <page title>.pdf              a page capture
    <slug>/attachments/<YYYY-MM-DD HHMM> - <file name>       a document, stamped with when it was fetched
    _unlisted/<folder name>/...                              a folder with no registry row

Stamp first so a listing sorts by time; the local files keep their own names. The
document stamp comes from `Attachments/.index.json` (`fetched`); a file the index does
not know gets its modification time.

Settings live in `~/.config/cam-webarchive/r2.env` (`KEY=value` lines, `chmod 600`;
`run.sh` passes them into the container, and the file is outside the repo):

    R2_ENDPOINT=https://<account id>.r2.cloudflarestorage.com
    R2_BUCKET=<bucket>
    R2_ACCESS_KEY_ID=<token id>
    R2_SECRET_ACCESS_KEY=<token secret>
    R2_PUBLIC_URL=https://<custom domain or r2.dev host>

The API token should be scoped to that one bucket with object read + write (no delete
needed — the sync never deletes). With `R2_PUBLIC_URL` set, `captures.json` gains each
page's newest-capture URL and the generated sources table links the *Archive* column.

The same image is what a cloud runner (GitHub Actions) or a home server would use;
only `run.sh`'s mounts would change.

## What's in each PDF

- One page per tab, named after the tab (bookmarks + page labels in Preview's sidebar).
  Pages are as tall as their content, whatever that is (Preview handles it; Acrobat
  refuses pages over 200in — set `MAX_PAGE_PX` to e.g. 18000 to split such tabs onto
  continuation pages labelled `Tab (2/3)` instead).
- Collapsed FAQ accordions (Bootstrap `.collapse`) and `<details>` are expanded before
  capture so hidden answers land in both the page and the text record.
- Every page carries the full page frame (top bar, hero, nav, engagement box,
  site footer) around its tab content. Charts and embedded iframes (maps,
  videos) are captured as images of their on-screen state; the hero banner is
  a 2x JPEG stored once and shared by all pages.
- Title metadata = page title + exact export time; source URL in metadata.
- Embedded attachment `capture.json`: per-tab plain text and outbound links
  (text + URL) captured at export time. Not visible in Preview (Acrobat/PDF Expert show attachments) — it's
  for change tracking, and a text record that survives independent of the
  PDF's text layer.
- Embedded attachment `changes.diff` (only when something changed): unified
  diff against the previous capture in the same folder.

## Linked files

Every capture also archives the documents the page links to. A link counts as a
document when it sits in the page's main content (not the header, nav, footer or
sidebar chrome) and either has a document extension (`.pdf`, `.docx`, `.xlsx`,
`.pptx`, `.zip`, `.kmz`, `.csv`) or looks like a download endpoint (`/download/`,
`?wpdmdl=`, `document.cfm`, `/documents/`…) and answers a HEAD request with a
document content-type. A PublicInput page's curated **Documents** list is
included as before. Files land in the page's `Attachments/` subfolder, keeping
the server's filename; `--no-docs` turns this off.

Re-checks are cheap by default: `Attachments/.index.json` records each URL's file
name, size, ETag, Last-Modified and SHA-256, and a later run only HEADs each URL —
skipped when the ETag matches (or, without one, size + Last-Modified, else size).
`--verify` downloads everything and compares SHA-256 instead. A file already on
disk from before the index existed is adopted when its size matches. A revised
file never overwrites: the new version is saved with a date stamp beside the old.

New downloads for one page are capped at 500 MB (`DOC_CAP_MB`) so a routine
`--all` never surprises you with a multi-gigabyte pull (an EIS with appendices is
~1.5 GB). Over the cap the page is still captured, the documents are skipped,
and the run's summary names the page; `--all-docs` lifts the cap for that run.
`--fetch` saves an explicit list of links into a page's `Attachments/` with no cap.
Embedded iframes (Google Drive previews, YouTube, maps) are recorded in each
capture's links as `[embedded]` entries, and Google Drive links in any form
(`file/d/…`, `/preview` embeds, `open?id=…`) are converted to direct downloads —
so an embedded Drive presentation can be fetched with `--fetch` using the URL
straight from `capture.json`;
a file already there and identical is skipped, a changed one gets a date stamp. Choosing which links matter is the job of the
`cam-archive-review` skill, which reads each capture's links and `changes.diff` and
proposes a shortlist — nothing is downloaded until you run the command it gives you.

## Change tracking

Each run first does a quick text-only walk of the tabs (no image settling or
screenshots; each tab is read until its text stops changing, no "Loading…"
placeholder remains, and no AJAX request is still in flight — a slow tab's
pending request would otherwise let leftover content pass for settled),
compares it with the most recent earlier PDF's `capture.json`,
and prints a per-tab summary plus the diff (a diff longer than
`MAX_DIFF_PRINT_LINES` is truncated in the log — the full diff is always embedded
in the new PDF as `changes.diff`). After the full capture, the diff is recomputed
from what will actually be stored: if the full capture turns out identical to the
previous one (the quick check misread a still-loading tab), nothing is written. **If nothing changed, no new PDF is
written** (pass `--force` to export anyway); only a changed page pays for the
full capture. Renamed tabs are paired by position and reported as
`Old → New (renamed)`; a genuinely new or removed tab counts as a change.
Dynamic UI noise (comment counters, "N characters remaining", relative
timestamps) is filtered via `NOISE_PATTERNS` at the top of the script — add a
pattern there when a false positive shows up.

## Notes

- Rendered by headless Chromium, not Safari — text is still vector/searchable.
- `PAGE_WIDTH_PX` / `HERO_JPEG_QUALITY` at the top of the script are the main knobs.
