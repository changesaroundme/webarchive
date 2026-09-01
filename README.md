# webarchive

Archive civic webpages as single PDFs with change tracking — the automated
alternative to the Safari bookmarklets (see "PublicInput PDF Bookmarklet" and
"AustinTexas.gov PDF Bookmarklet" in Archive - Changes Around Me/Tooling).

- **PublicInput (Speak Up Austin) project pages** → one content-sized PDF page per tab.
- **Any other page** (austintexas.gov project pages, etc.) → one content-sized PDF page,
  with collapsed sections (Drupal accordions, Bootstrap collapse, `<details>`) expanded
  and fixed headers pinned into the flow first.

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
    .venv/bin/python archive_page.py --all                    # re-check every archived page; writes only what changed

Captures land in `Archive - Changes Around Me/Tooling/Public Input Export/<page title>/`
(PublicInput pages) or `Tooling/Website Export/<page title>/` (everything else) by default,
named `<page title> - <2026-08-30 17-42>.pdf` (export date + time). `--all` re-checks
every page under both.

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
- Embedded attachment `capture.json`: per-tab plain text captured at export
  time. Not visible in Preview (Acrobat/PDF Expert show attachments) — it's
  for change tracking, and a text record that survives independent of the
  PDF's text layer.
- Embedded attachment `changes.diff` (only when something changed): unified
  diff against the previous capture in the same folder.

## Change tracking

Each run first does a quick text-only walk of the tabs (no image settling or
screenshots), compares it with the most recent earlier PDF's `capture.json`,
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
