# webarchive

Archive civic webpages as single PDFs with change tracking — the automated
alternative to the Safari bookmarklets (see "PublicInput PDF Bookmarklet" and
"AustinTexas.gov PDF Bookmarklet" in Archive - Changes Around Me/Tooling).

- **PublicInput (Speak Up Austin) project pages** → one content-sized PDF page per tab.
- **Any other page** (austintexas.gov project pages, ArcGIS StoryMaps, etc.) → one
  content-sized PDF page: the page is scrolled through so lazy images load, collapsed
  sections (Drupal accordions, Bootstrap collapse, `<details>`) are expanded, overlays are
  hidden and fixed headers pinned into the flow first.

The script decides which kind it's looking at by itself; the command is the same.

## One-time setup

    cd webarchive
    python3 -m venv .venv
    .venv/bin/pip install playwright pikepdf
    .venv/bin/playwright install chromium

## Usage

    .venv/bin/python archive_page.py https://www.speakupaustin.org/centralcity
    .venv/bin/python archive_page.py <url> "My Name.pdf"      # explicit output path
    .venv/bin/python archive_page.py <url> --out-dir=~/some/dir
    .venv/bin/python archive_page.py <url> --original-hero     # keep the hero's source PNG (+~7 MB)
    .venv/bin/python archive_page.py <url> --force             # export even if nothing changed
    .venv/bin/python archive_page.py <url> --width=1440
    .venv/bin/python archive_page.py <url> --no-docs           # skip auto-download of the Documents list
    .venv/bin/python archive_page.py --all                    # re-check every archived page; writes only what changed
    .venv/bin/python archive_page.py --fetch "Central City District Plan" 'https://…/report.pdf' 'https://…/boards.pdf'
                                                              # save linked files into that page's folder

Every capture lands in `Archive - Changes Around Me/Tooling/Web Archive/<page title>/`,
named `<page title> - <2026-08-30 1742>.pdf` (export date + time) — one flat archive,
whatever the site. The owning organization (PublicInput customer id → `PUBLICINPUT_ORGS`
at the top of the script; hostname for other sites) and the source URL are recorded in each
capture's metadata; organizing happens in the knowledge base, not in the folders.
`--all` re-checks every page under the root. If two different pages ever share a title,
the change check refuses to use the other page's capture as a baseline and says so.

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

A PublicInput page's curated **Documents** list (the sidebar file list) is archived
automatically: each new capture also downloads those files into the page's
`Attachments/` subfolder, skipping ones already there unchanged (`--no-docs` turns
this off). For everything else the archive captures pages, not the documents they
link to. `--fetch` saves chosen links (reports, open-house boards, memos, feedback
summaries) into the same `Attachments/` subfolder, keeping the server's filename;
a file already there and identical is skipped, a changed one gets a date stamp. Choosing which links matter is the job of the
`cam-archive-review` skill, which reads each capture's links and `changes.diff` and
proposes a shortlist — nothing is downloaded until you run the command it gives you.

## Change tracking

Each run first does a quick text-only walk of the tabs (no image settling or
screenshots; each tab is read until its text stops changing and no "Loading…"
placeholder remains), compares it with the most recent earlier PDF's `capture.json`,
and prints a per-tab summary plus the diff. **If nothing changed, no new PDF is
written** (pass `--force` to export anyway); only a changed page pays for the
full capture. Renamed tabs are paired by position and reported as
`Old → New (renamed)`; a genuinely new or removed tab counts as a change.
Dynamic UI noise (comment counters, "N characters remaining", relative
timestamps) is filtered via `NOISE_PATTERNS` at the top of the script — add a
pattern there when a false positive shows up.

## Notes

- Rendered by headless Chromium, not Safari — text is still vector/searchable.
- `PAGE_WIDTH_PX` / `HERO_JPEG_QUALITY` at the top of the script are the main knobs.
