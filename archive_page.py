#!/usr/bin/env python3
"""Archive a civic webpage as a single PDF with the captured text embedded and
a change report against the previous capture.

Two kinds of page are handled:
  * PublicInput (Speak Up Austin) project pages — one content-sized PDF page per tab.
  * Any other page (e.g. austintexas.gov project pages) — one content-sized PDF page,
    with collapsed sections (accordions, <details>) expanded first.
  * ArcGIS Experience Builder apps — every page the app's nav links to, one PDF
    page each, as tabs of one capture.

Usage:
    python archive_page.py URL [output.pdf] [options]
    python archive_page.py --all [options]      # re-check every archive page in sources.csv
    python archive_page.py --due [options]      # only the pages whose check interval has elapsed
                                                # (what the daily launchd job runs)
    python archive_page.py --fetch "<page title>" URL [URL ...]
                                                # save linked files (reports, boards, memos) into that page's folder

Options:
    --width=PX          layout width (default 1800; keep >= 1200 or the sidebar collapses)
    --out-dir=DIR       where captures go (default: Archive vault, Tooling/Web Archive/<page title>/)
    --original-hero     keep the hero banner's source image (lossless, ~7 MB once)
                        instead of the default 2x JPEG screenshot (~0.8 MB)
    --force             write a PDF even when the text is identical to the previous capture
                        (default: unchanged pages are not re-exported)
    --no-docs           don't auto-download the documents a page links to (default: every
                        PDF/Office/zip file linked from the page's main content — plus a
                        PublicInput page's "Documents" list — is saved into Attachments/)
    --all-docs          lift the per-page download cap (DOC_CAP_MB) for this run
    --verify            re-download every linked document and compare it byte-for-byte
                        (default: a HEAD request per file, skipped when ETag / size /
                        Last-Modified match what Attachments/.index.json recorded)
    --all               batch mode: every row of calendars/sources.csv with archive=yes and
                        status=active is re-checked in turn (the folder is still the page
                        title; the registry only supplies the list)
    --due               like --all, but a page is skipped until its `check` interval has
                        elapsed since the last check — inside a row's `expect` window the
                        interval tightens to daily. Both modes write calendars/docs/captures.json
                        (per slug: last checked, newest capture) for the public sources table.
    --registry=FILE     sources.csv to read (default: ../calendars/sources.csv next to this repo)
    --sync              only mirror the archive to object storage (r2sync.py; needs the R2_*
                        settings) — with --dry-run to list what would upload, --verify to
                        compare sizes of what is already there
    --verbose           in batch mode, print the full per-tab diffs and every unchanged document
                        as a single-page run does (batch output is one line per tab and per
                        saved or failed file; the full diff is always inside the PDF)

Requires: pip install playwright pikepdf && playwright install chromium

How it renders: instead of printing PublicInput's fragile app layout
(vbox/hbox/push-full scaffolding, which misplaces the footer in PDF
rendering), it snapshots each tab's content — converting chart canvases
and embedded iframes (e.g. the interactive map) to images of their
on-screen state — then rebuilds each page as a plain linear document
(page header/hero/nav + tab content + site footer) and prints that.

Change tracking: each PDF carries an embedded attachment `capture.json`
(title, URL, export time, per-tab plain text). On each run the most recent
previous capture in the output folder is read back and diffed per tab; the
diff is printed and, when non-empty, also embedded as `changes.diff`.
"""
import base64
import difflib
import hashlib
import io
import json
import os
import re
import sys
import time
import warnings
import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote

from playwright.sync_api import sync_playwright
import pikepdf

# Intermediate certificates some hosts fail to send (certs/fetch-chain.sh
# collects them). Playwright's download client runs on Node, which does not
# fetch missing links itself; handing it the file makes those downloads verify.
_EXTRA_CA = Path(__file__).resolve().parent / "certs" / "extra-ca.pem"
if _EXTRA_CA.is_file():
    os.environ.setdefault("NODE_EXTRA_CA_CERTS", str(_EXTRA_CA))

PAGE_WIDTH_PX = 1800          # layout width; keep >=1200 or the sidebar column collapses. ~1800 matches Ian's Safari exports
PX_PER_IN = 96
SETTLE_MS = 1800              # wait after tab content arrives (images, charts, embeds) — full capture
QUICK_SETTLE_MS = 300         # polling interval while waiting for a tab's text to stop changing (change-check pass)
TAB_TIMEOUT_MS = 12000
HERO_JPEG_QUALITY = 90        # hero banner is captured once at 2x (Retina) and shared by all pages
MAX_PAGE_PX = 0               # 0 = one page per tab regardless of height (Preview is fine with very tall pages).
                              # Set to e.g. 18000 to split taller tabs onto continuation pages (Acrobat caps pages at 200in = 19200px).
DOC_CAP_MB = 500              # per page: new documents beyond this need --all-docs (an EIS is ~1.5 GB)
DOC_EXT_RE = re.compile(r"\.(pdf|docx?|xlsx?|pptx?|zip|kmz|kml|csv)(?:[?#]|$)", re.IGNORECASE)
# links that smell like a download even without an extension (LCRA's ?wpdmdl=, EDIMS document.cfm)
DOC_HINT_RE = re.compile(r"download|wpdmdl=|document\.cfm|/documents?/|/files?/|/uploads/|/media/", re.IGNORECASE)
DOC_TYPES = ("application/pdf", "application/msword", "application/vnd.", "application/zip",
             "application/x-zip", "application/octet-stream", "text/csv")
MAX_DIFF_PRINT_LINES = 40     # a longer diff is truncated in the log; the full diff is always embedded as changes.diff
# Where things live. Inside the container (see Dockerfile / run.sh) the two
# folders are bind-mounted and named by CAM_ARCHIVE_ROOT / CAM_CALENDARS_REPO;
# on the Mac the defaults are the vault's archive folder and the sibling checkout.
ARCHIVE_TOOLING = (Path.home() / "Library/Mobile Documents/iCloud~md~obsidian/Documents"
                   / "Archive - Changes Around Me/Tooling")
ARCHIVE_ROOT = Path(os.environ.get("CAM_ARCHIVE_ROOT") or ARCHIVE_TOOLING / "Web Archive")
                                                   # every page: <root>/<page title>/<title> - <stamp>.pdf
# The page registry lives in the calendars repo (a sibling checkout); --all and
# --due read it, and the capture-times file goes back next to it so the build's
# generated sources table can show an Archive column.
CALENDARS_REPO = Path(os.environ.get("CAM_CALENDARS_REPO") or Path(__file__).resolve().parent.parent / "calendars")
REGISTRY = CALENDARS_REPO / "sources.csv"
CAPTURES = CALENDARS_REPO / "docs" / "captures.json"
CHECK_INTERVALS = {"twice daily": 0.5, "daily": 1, "weekly": 7, "monthly": 30}
CHECK_EVERY_RE = re.compile(r"every (\d+) (day|week|month)s?", re.IGNORECASE)
# PublicInput customer ids -> organization name, recorded in each capture's
# metadata (the archive itself stays flat — organizing happens in the KB).
PUBLICINPUT_ORGS = {
    "110": "City of Austin",
    "2658": "CapMetro",
}
# Lines matching these are ignored when diffing (dynamic UI noise, not content changes)
NOISE_PATTERNS = [
    r"^Loading\b.*$",                  # "Loading Comments" etc. — placeholders the quick pass can catch mid-load
    r"^\d+ characters remaining$",
    r"^\d+ (comments?|responses?|participants?)$",
    r"\b\d+ (seconds?|minutes?|hours?|days?) ago\b",
]

# Expand collapsed content (FAQ accordions use Bootstrap .collapse; also <details>)
# so hidden answers make it into both the text record and the rendered page.
EXPAND_JS = """
(content) => {
  // Screen-reader-only text is not content: PublicInput's #survey-step-heading
  // (visually-hidden) reads the active tab's name and started lagging behind
  // tab clicks in Sep 2026, so every tab's text began with the first tab's name.
  for (const h of content.querySelectorAll('.visually-hidden, .sr-only, .screen-reader-text')) h.style.setProperty('display', 'none', 'important');
  for (const e of content.querySelectorAll('.collapse:not(.show)')) { e.classList.add('show', 'in'); e.style.height = 'auto'; }
  for (const t of content.querySelectorAll('.collapsed')) t.classList.remove('collapsed');
  for (const d of content.querySelectorAll('details')) d.open = true;
  // ARIA accordions (e.g. TxDOT's AEM "melodeon", which allows only one open panel
  // at a time — so panels are forced open structurally, never by clicking):
  for (const b of content.querySelectorAll('[class*="accordion"] [aria-expanded="false"], [data-melodeon-btn][aria-expanded="false"]')) {
    b.setAttribute('aria-expanded', 'true');
    const p = (b.getAttribute('aria-controls') && document.getElementById(b.getAttribute('aria-controls'))) || b.nextElementSibling;
    if (!p) continue;
    p.hidden = false;
    p.style.setProperty('display', 'block', 'important');
    p.style.setProperty('max-height', 'none', 'important');
    p.style.setProperty('opacity', '1', 'important');
    // the site's own open animation would also fade the content in — force that end
    // state, or the panel prints as reserved-but-blank space (TxDOT's .acc-panel-inner)
    for (const el of p.querySelectorAll('*')) {
      const cs = getComputedStyle(el);
      if (parseFloat(cs.opacity) === 0) el.style.setProperty('opacity', '1', 'important');
      if (cs.visibility === 'hidden') el.style.setProperty('visibility', 'visible', 'important');
      if (cs.maxHeight === '0px') el.style.setProperty('max-height', 'none', 'important');
    }
  }
}
"""

# Change-check pass: just the tab's visible text (after expansion), or null.
TEXT_JS = "() => { const c = document.querySelector('section.project-content'); if (!c) return null; (" + EXPAND_JS.strip() + ")(c); return c.innerText; }"

# Survey pages: the "answer required questions before continuing" gate is purely
# client-side — POST /Project/LiveStep returns any step on request, unanswered.
# Fetch step i and swap it into the step container, hiding the survey chrome.
LOAD_STEP_JS = """
async (i) => {
  const UI = (window.PageUIObjects || [])[0];
  if (!UI || !UI.config) return 'no PageUIObjects[0].config - has PublicInput changed?';
  const postData = Object.assign({loadStepIndex: i, currentStepIndex: 0, performingSkip: false,
                                  isMeetingSignIn: false}, UI.config);
  const data = await new Promise((res) => jQuery.post('/Project/LiveStep', postData)
    .done(res).fail(x => res({result: 'HTTP ' + x.status})));
  const box = document.getElementById('surveyStepContent');
  if (!box) return 'no #surveyStepContent';
  if (data.html) box.innerHTML = data.html;
  else box.innerHTML = '<p>(survey step ' + (i + 1) + ' returned no content: ' + (data.result || '?') + ')</p>';
  // The site's own step loader calls this after inserting the HTML: it hydrates
  // every question widget. Without it, matrix/grid questions sit on
  // "Loading question..." for good (Great Streets survey, Sep 2026).
  if (data.html && UI.initPollsOnPage) { try { UI.initPollsOnPage(); } catch (e) {} }
  for (const b of document.querySelectorAll('.step-continue-button, .step-back-button, .errorField'))
    b.style.display = 'none';
  return data.html ? 'SUCCESS' : (data.result || 'EMPTY');
}
"""

# Generic pages: expand everything collapsed, pin fixed/sticky chrome into the
# flow, hide floating widgets. Returns the page text. Used for the change check
# and (with iframe screenshots swapped in) for the render.
GENERIC_PREP_JS = """
(iframeShots) => {
  const q = (s) => [...document.querySelectorAll(s)];
  q('.ckeditor-accordion-container dt').forEach(d => d.classList.add('active'));   // Drupal accordions (austintexas.gov)
  q('.ckeditor-accordion-container dd').forEach(d => d.style.display = 'block');
  (""" + EXPAND_JS.strip() + """)(document.body);
  // overlays (lightboxes, dialogs, cookie banners) must be hidden, not pinned into the flow
  for (const e of q('[role="dialog"],[aria-modal="true"],.modal,.full-screen-modal,.ReactModalPortal,[class*="lightbox"],[class*="cookie"]')) e.style.display = 'none';
  for (const e of q('*')) {
    const s = getComputedStyle(e);
    if (s.position !== 'fixed' && s.position !== 'sticky') continue;
    if (e.style.display === 'none') continue;
    if (s.visibility === 'hidden' || parseFloat(s.opacity) === 0) e.style.display = 'none';  // invisible overlays would become blank bands
    else e.style.position = 'static';
  }
  // Freeze viewport-relative heights: in page.pdf() the CSS viewport is the PAGE,
  // so a 100vh cover section (StoryMaps etc.) would re-inflate to the full page
  // height and push everything else off the single page. Pin anything roughly
  // viewport-sized to the pixel height it has on screen right now.
  const vh = window.innerHeight;
  for (const e of q('*')) {
    if (e === document.documentElement || e === document.body) continue;
    const oh = e.offsetHeight;
    if (oh < vh * 0.5 || oh > vh * 3.05) continue;
    if (getComputedStyle(e).display === 'inline') continue;
    e.style.setProperty('height', oh + 'px', 'important');
    e.style.setProperty('min-height', '0', 'important');
    e.style.setProperty('max-height', 'none', 'important');
  }
  for (const e of q('[class*="userway"],.grecaptcha-badge,[class*="VIpgJd"],.asw-menu-btn,.asw-container,[class*="print-wrapper"]')) e.style.display = 'none';
  // ArcGIS Experience Builder "section" widgets show one "view" at a time behind
  // Previous/Next arrows (the Airport Corridor page's 7-slide summary). Every view
  // is in the DOM, just hidden — stack them so the print shows all of them, and
  // let the ancestors grow (the height-freeze above pinned them).
  for (const sec of q('.section-content')) {
    const views = [...sec.children].filter(c => c.classList.contains('view-content'));
    if (views.length < 2) continue;
    const H = sec.getBoundingClientRect().height;
    if (!H) continue;
    sec.style.setProperty('height', (H * views.length) + 'px', 'important');
    for (const v of views) {
      for (const [k, val] of [['display', 'block'], ['position', 'relative'], ['height', H + 'px'], ['top', '0'], ['left', '0']])
        v.style.setProperty(k, val, 'important');
    }
    for (let a = sec.parentElement; a && a !== document.body; a = a.parentElement) {
      a.style.setProperty('height', 'auto', 'important');
      a.style.setProperty('max-height', 'none', 'important');
      a.style.setProperty('overflow', 'visible', 'important');
    }
  }
  if (iframeShots) q('iframe, canvas').forEach((f, i) => {   // canvases too: WebGL maps print blank
    if (!iframeShots[i]) return;
    const m = document.createElement('img');
    m.src = 'data:image/png;base64,' + iframeShots[i];
    m.width = f.clientWidth || f.offsetWidth; m.height = f.clientHeight || f.offsetHeight;
    m.style.maxWidth = '100%'; f.replaceWith(m);
  });
  for (const el of [document.documentElement, document.body]) {
    el.style.setProperty('overflow', 'visible', 'important');
    el.style.setProperty('height', 'auto', 'important');
  }
  return document.body.innerText;
}
"""

LINKS_JS = """
() => [...document.querySelectorAll('a[href]')]
  .filter(a => /^https?:/.test(a.href) && !a.href.includes('#'))
  .map(a => ({text: a.textContent.trim().slice(0, 120), href: a.href,
              chrome: !!a.closest('header, nav, footer, aside, [role=navigation], [role=banner], [role=contentinfo]')
                      || (!!document.querySelector('main, [role=main], article') && !a.closest('main, [role=main], article'))}))
  .concat([...document.querySelectorAll('iframe[src], embed[src], object[data]')]
    .map(f => f.src || f.data).filter(s => /^https?:/.test(s))
    .map(s => ({text: '[embedded]', href: s})))
"""

# Scroll through the page so lazy-loaded images and sections actually load.
SCROLL_JS = """
async () => {
  const h = () => Math.max(document.body.scrollHeight, document.documentElement.scrollHeight);
  for (let y = 0; y < h(); y += 800) { window.scrollTo(0, y); await new Promise(r => setTimeout(r, 350)); }
  window.scrollTo(0, 0);
}
"""

# Snapshot the current tab: clone its content, swapping live canvases and
# iframes for images (canvas pixels don't survive cloning; iframes reload
# blank when re-inserted). iframe screenshots are passed in from Python.
SNAP_JS = """
(args) => {
  const [iframeShots, heroShot] = args;
  window.__piSnaps = window.__piSnaps || [];
  window.__piTexts = window.__piTexts || [];
  if (!window.__piFrame) {
    // Every page keeps the full page frame: everything visible before the tab
    // content (top bar, hero, nav, engagement box) and after it (site footer),
    // captured structurally from the page's own order rather than by guessing
    // class names, which vary between PublicInput page templates.
    // Frame parts get their live pixel height frozen: the hero's height comes
    // from a percentage padding that Chromium's PDF renderer resolves to zero.
    // <style> blocks living in the body are carried along — one of them paints
    // the hero banner image. The hero itself (.header-div) is swapped for a
    // 2x JPEG screenshot: its native background is a 4000x1000 lossless PNG
    // (~8 MB). The merge step dedupes images so it's stored once for all pages.
    const freeze = (e) => {
      if (heroShot && e.classList.contains('header-div')) {
        const m = document.createElement('img');
        m.src = 'data:image/jpeg;base64,' + heroShot;
        m.width = e.offsetWidth; m.height = e.offsetHeight; m.style.display = 'block';
        return m;
      }
      const c = e.cloneNode(true); c.style.position = 'static';
      c.style.height = e.offsetHeight + 'px'; c.style.boxSizing = 'border-box';
      c.style.paddingBottom = '0'; return c; };
    window.__piStyles = [...document.body.querySelectorAll('style')].map(st => st.cloneNode(true));
    window.__piHeader = []; window.__piFooter = [];
    const hd = document.querySelector('header.default-hub-header');
    if (hd) window.__piHeader.push(freeze(hd));
    const pf = document.querySelector('.push-full');
    const c0 = document.querySelector('section.project-content');
    if (pf && c0) {
      let after = false;
      for (const ch of pf.children) {
        if (ch === c0 || ch.contains(c0)) { after = true; continue; }
        if (ch.offsetHeight > 0 && !/^(SCRIPT|STYLE)$/.test(ch.tagName) && !ch.classList.contains('modal'))
          (after ? window.__piFooter : window.__piHeader).push(freeze(ch));
      }
    }
    const ft = document.querySelector('.project-footer-html');
    if (ft && ft.offsetHeight > 0) window.__piFooter.push(freeze(ft));
    window.__piFrame = true;
  }
  const content = document.querySelector('section.project-content');
  if (!content) return -1;
  (""" + EXPAND_JS.strip() + """)(content);
  window.__piTexts.push(content.innerText);
  window.__piLinks = window.__piLinks || [];
  window.__piLinks.push([...content.querySelectorAll('a[href]')]
    .filter(a => /^https?:/.test(a.href) && !a.href.includes('#'))
    .map(a => ({text: a.textContent.trim().slice(0, 120), href: a.href}))
    .concat([...content.querySelectorAll('iframe[src], embed[src], object[data]')]
      .map(f => f.src || f.data).filter(s => /^https?:/.test(s))
      .map(s => ({text: '[embedded]', href: s}))));   // Drive/YouTube embeds are content too — record them
  const cl = content.cloneNode(true);
  const swap = (liveList, cloneList, srcFor) => {
    liveList.forEach((live, i) => {
      try {
        const src = srcFor(live, i);
        if (!src) return;
        const m = document.createElement('img');
        m.src = src;
        m.width = live.clientWidth || live.offsetWidth;
        m.height = live.clientHeight || live.offsetHeight;
        m.style.maxWidth = '100%';
        cloneList[i].replaceWith(m);
      } catch (e) {}
    });
  };
  swap([...content.querySelectorAll('canvas')], [...cl.querySelectorAll('canvas')],
       (c) => c.toDataURL());
  swap([...content.querySelectorAll('iframe')], [...cl.querySelectorAll('iframe')],
       (f, i) => iframeShots[i] ? 'data:image/png;base64,' + iframeShots[i] : null);
  window.__piSnaps.push(cl);
  return window.__piSnaps.length;
}
"""

# Rebuild the page as a linear document for one tab: styles + frame + content.
# No measurement here — that happens after a settle so late-decoding images count.
BUILD_JS = """
(idx) => {
  document.body.replaceChildren(...window.__piStyles, ...window.__piHeader,
                                window.__piSnaps[idx], ...window.__piFooter);
  for (const el of [document.documentElement, document.body]) {
    el.style.setProperty('overflow', 'visible', 'important');
    el.style.setProperty('height', 'auto', 'important');
  }
  document.body.style.background = '#fff';
  document.body.style.margin = '0';
  for (const i of document.body.querySelectorAll('img')) i.style.maxWidth = '100%';
}
"""

MEASURE_JS = """
() => {
  let b = Math.max(document.body.scrollHeight, document.documentElement.scrollHeight);
  for (const e of document.body.querySelectorAll('*')) {
    const r = e.getBoundingClientRect();
    if (r.height > 0) b = Math.max(b, r.bottom + window.scrollY);
  }
  return Math.ceil(b);
}
"""


def dedupe_images(pdf):
    """Chromium embeds a fresh copy of every image per page; point identical
    images (hero banner, logos) at one shared object instead."""
    seen = {}
    for page in pdf.pages:
        xobjects = page.Resources.get("/XObject")
        if xobjects is None:
            continue
        for name in list(xobjects.keys()):
            obj = xobjects[name]
            if obj.get("/Subtype") != "/Image":
                continue
            key = hashlib.sha256(obj.read_raw_bytes()).hexdigest()
            if key in seen and seen[key].objgen != obj.objgen:
                xobjects[name] = seen[key]
            else:
                seen.setdefault(key, obj)


def clean_lines(text):
    """Text lines for diffing: stripped, non-empty, UI noise removed."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        # PublicInput appends its loading placeholder to the heading itself:
        # "About the ProjectLoading About the Project" -> "About the Project"
        line = re.sub(r"^(.*?)Loading \1$", r"\1", line)
        if not line or any(re.search(p, line) for p in NOISE_PATTERNS):
            continue
        out.append(line)
    return out


def previous_capture(out_dir, safe_title, current_path, url=None):
    """Most recent earlier PDF for this page that carries a capture.json.
    A capture of a different URL (two pages sharing a title in the flat
    archive) is never used as the baseline."""
    candidates = sorted(p for p in out_dir.glob(f"{safe_title} - *.pdf") if p != current_path)
    for path in reversed(candidates):
        try:
            with pikepdf.open(path) as pdf:
                if "capture.json" in pdf.attachments:
                    data = json.loads(pdf.attachments["capture.json"].get_file().read_bytes())
                    if url and data.get("url") not in (None, url):
                        print(f"  WARNING: {path.name} is a capture of a different page ({data.get('url')}) — "
                              f"not used as baseline; consider --out-dir to keep them apart")
                        continue
                    return path, data
        except Exception as e:  # unreadable/odd file: skip it, keep looking
            print(f"  (skipping {path.name}: {e})")
    return None, None


def diff_captures(prev, cur):
    """Per-tab unified diff. Tabs pair by name; leftovers pair by position
    (a renamed tab), and anything still unpaired is genuinely new/removed.
    Returns (summary_lines, diff_text) — diff_text empty means no content change."""
    prev_tabs, cur_tabs = list(prev["tabs"]), list(cur["tabs"])
    pairs, used_prev = [], set()
    for c in cur_tabs:
        for i, p in enumerate(prev_tabs):
            if i not in used_prev and p["name"] == c["name"]:
                pairs.append((p, c)); used_prev.add(i); break
        else:
            pairs.append((None, c))
    leftover_prev = [p for i, p in enumerate(prev_tabs) if i not in used_prev]
    for k, (p, c) in enumerate(pairs):          # renames: unmatched cur ↔ unmatched prev, in order
        if p is None and leftover_prev:
            pairs[k] = (leftover_prev.pop(0), c)
    pairs += [(p, None) for p in leftover_prev]

    summary, chunks = [], []
    for p, c in pairs:
        a = clean_lines(p["text"]) if p else []
        b = clean_lines(c["text"]) if c else []
        label = (c or p)["name"]
        if p and c and p["name"] != c["name"]:
            label = f"{p['name']} → {c['name']} (renamed)"
        d = list(difflib.unified_diff(a, b, fromfile=f"{label} @ {prev['exported']}",
                                      tofile=f"{label} @ {cur['exported']}", lineterm="", n=1))
        if not c:
            summary.append(f"  {label}: TAB REMOVED"); chunks.append("\n".join(d))
        elif not p:
            summary.append(f"  {label}: NEW TAB"); chunks.append("\n".join(d))
        elif d:
            added = sum(1 for l in d[2:] if l.startswith("+"))
            removed = sum(1 for l in d[2:] if l.startswith("-"))
            summary.append(f"  {label}: +{added} / -{removed} lines"); chunks.append("\n".join(d))
        else:
            summary.append(f"  {label}: unchanged")
    return summary, "\n\n".join(chunks)


def settled_text(page):
    """The tab's text once it has stopped changing: two reads QUICK_SETTLE_MS apart
    agree, no 'Loading…' placeholder remains, and no AJAX request is in flight
    (capped at TAB_TIMEOUT_MS). The in-flight check matters: PublicInput swaps tab
    content via jQuery, and while a slow tab's request is still pending the section
    shows a perfectly stable leftover (placeholder heading + sidebar) that would
    otherwise pass for settled — and the late response can then land while the NEXT
    tab is being read, crediting one tab's text to another. Static tabs still
    finish in two reads."""
    deadline = time.time() + TAB_TIMEOUT_MS / 1000
    last = None
    while True:
        page.wait_for_timeout(QUICK_SETTLE_MS)
        text = page.evaluate(TEXT_JS)
        if text is None:
            return None
        busy = page.evaluate("() => window.jQuery ? jQuery.active : 0")
        if text == last and not busy and not re.search(r"^Loading\b", text, re.M):
            return text
        if time.time() > deadline:
            return text
        last = text


TRANSIENT_NET = ("ERR_NETWORK_CHANGED", "ERR_CONNECTION_RESET", "ERR_NAME_NOT_RESOLVED",
                 "ERR_CONNECTION_CLOSED", "ERR_INTERNET_DISCONNECTED", "ERR_TIMED_OUT")


def navigate(page, url=None, attempts=3):
    """page.goto(url) — or page.reload() when url is None — retried on the
    transient network errors Chromium raises when the link blips (the container's
    network coming up just after the service starts, Wi-Fi roaming, a VPN
    reconnecting): ERR_NETWORK_CHANGED and friends. Anything else raises at once."""
    for attempt in range(1, attempts + 1):
        try:
            if url is None:
                return page.reload(wait_until="load", timeout=60000)
            return page.goto(url, wait_until="load", timeout=60000)
        except Exception as e:
            if attempt == attempts or not any(code in str(e) for code in TRANSIENT_NET):
                raise
            print(f"  network blip ({str(e).split(chr(10))[0][:80]}); retrying in 5 s")
            page.wait_for_timeout(5000)


def settled_dom(page):
    """Full-pass settle before a tab is snapshotted: no AJAX in flight, no
    'Loading…' placeholder in the text, and the DOM the same size for SETTLE_MS
    (polled every QUICK_SETTLE_MS, capped at TAB_TIMEOUT_MS). A survey step arrives as
    a shell whose questions and comment boxes then load themselves — a fixed
    wait printed 'Loading question…' and skeleton bars (Sep 2026)."""
    deadline = time.time() + TAB_TIMEOUT_MS / 1000
    last, quiet_since = None, time.time()
    while time.time() < deadline:
        page.wait_for_timeout(QUICK_SETTLE_MS)
        size, busy, loading = page.evaluate(
            "() => [document.body.innerHTML.length, window.jQuery ? jQuery.active : 0, "
            "/^Loading\\b/m.test(document.body.innerText)]")
        if size != last or busy or loading:
            quiet_since = time.time()              # something moved: restart the quiet window
        elif time.time() - quiet_since >= SETTLE_MS / 1000:
            return                                 # widgets that load in stages arrive a second apart
        last = size


def clean_title(title):
    """'Sir Swante Palm ... | Austin Parks | AustinTexas.gov' -> 'Sir Swante Palm ...';
    'Central City District Plan - PublicInput' -> 'Central City District Plan'."""
    t = title.split(" | ")[0]
    t = re.sub(r"\s*-\s*(PublicInput|Austin Transit Partnership)$", "", t)
    t = re.sub(r"[^\w\- ]+", "", t)
    return re.sub(r"\s+", " ", t).strip() or "page"


def screenshot_iframes(page, selector):
    """Screenshot each iframe matching selector as it looks on screen (None if it can't be)."""
    frames = page.locator(selector)
    shots = []
    for i in range(frames.count()):
        try:
            frames.nth(i).scroll_into_view_if_needed(timeout=3000)
            page.wait_for_timeout(600)
            shots.append(base64.b64encode(frames.nth(i).screenshot(timeout=5000)).decode())
        except Exception:
            shots.append(None)
    return shots


def print_page(page, width):
    """Print the current document as one content-sized PDF page (or several if MAX_PAGE_PX caps it)."""
    height_px = page.evaluate(MEASURE_JS) + 40
    capped = bool(MAX_PAGE_PX) and height_px > MAX_PAGE_PX
    buf = page.pdf(
        width=f"{width / PX_PER_IN}in",
        height=f"{(min(height_px, MAX_PAGE_PX) if capped else height_px) / PX_PER_IN}in",
        print_background=True,
        page_ranges=None if capped else "1",
    )
    with pikepdf.open(io.BytesIO(buf)) as part:
        n_pages = len(part.pages)
    return buf, height_px, n_pages


def detect_org(page, url, is_publicinput):
    """Organization for the capture metadata: PublicInput customer id lookup, else hostname."""
    if is_publicinput:
        m = re.search(r"custId[\"'=:\s]+(\d+)", page.content())
        cust = m.group(1) if m else None
        return PUBLICINPUT_ORGS.get(cust, f"PublicInput customer {cust or 'unknown'}")
    return re.sub(r"^www\.", "", url.split("/")[2])


OVER_CAP = []                 # (url, MB) of pages whose new documents exceeded DOC_CAP_MB this run
DOC_FAILURES = []             # (page folder, url, reason) for every document that could not be fetched this run
LAST = {}                     # out_dir / safe_title of the page archive() handled last (batch bookkeeping)
VERBOSE = True                # single-page runs show diffs and every document; batch runs summarise (--verbose to restore)


def _over_cap(url, mb):
    if mb:
        OVER_CAP.append((url, mb))


def _doc_failed(folder, url, reason):
    reason = str(reason).splitlines()[0][:160]         # Playwright errors run to many lines
    DOC_FAILURES.append((folder.parent.name, url, reason))
    print(f"  FAILED {url}: {reason}")


def archive(url, explicit_out=None, out_dir=None, width=PAGE_WIDTH_PX,
            original_hero=False, force=False, fetch_docs=True, all_docs=False, verify=False,
            root=None):
    """Capture one page, then bring its linked documents up to date. Returns
    the written path, or None when unchanged. out_dir is this page's folder;
    root (batch mode) is the archive root the page-title folder goes under.

    The document sync opens its own Playwright, so it must run after the
    capture's Playwright context has closed — nesting the two raises
    "Sync API inside the asyncio loop"."""
    written, record, folder = _capture(url, explicit_out, out_dir, width, original_hero, force, root)
    if fetch_docs and record is not None:
        _over_cap(url, sync_docs(folder, record, all_docs, verify))
    return written


def _capture(url, explicit_out, out_dir, width, original_hero, force, root):
    """The capture itself. Returns (written path or None, the capture record
    whose documents to sync — the previous one when unchanged — and the page folder)."""
    url = url.split("#")[0]   # a #tab-... fragment changes nothing (tabs are walked in nav order);
                              # stripping it keeps the stored URL stable for change detection
    nav_url = url
    m = re.match(r"(https://storymaps\.arcgis\.com/stories/[A-Za-z0-9]+)/?$", url)
    if m:
        # StoryMaps' own linear print rendition: full content in document order, no
        # scrollytelling virtualization (the interactive view prints scrambled).
        # The record keeps the story's normal URL.
        nav_url = m.group(1) + "/print"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": 1000},
                                device_scale_factor=2)  # iframe screenshots at 2x so map embeds stay crisp
        page.emulate_media(media="screen")   # site's @media print styles hide the sidebar
        navigate(page, nav_url)
        page.wait_for_timeout(SETTLE_MS * 2)

        title = page.title()
        safe_title = clean_title(title)
        is_publicinput = page.locator("section.project-content").count() > 0
        org = detect_org(page, url, is_publicinput)
        stamp = datetime.now().strftime("%Y-%m-%d %H%M")   # e.g. 2026-08-30 1742
        if explicit_out:
            out = Path(explicit_out).expanduser()
            out_dir = out.parent
        else:
            out_dir = out_dir or ((root or ARCHIVE_ROOT) / safe_title)
            out = out_dir / f"{safe_title} - {stamp}.pdf"
        out_dir.mkdir(parents=True, exist_ok=True)
        LAST.update(out_dir=out_dir, safe_title=safe_title)

        if not is_publicinput:
            result = archive_generic(page, browser, url, title, safe_title, stamp, out, out_dir, width, force, org)
            return (result[0], result[1], out_dir) if result else (None, None, out_dir)

        labels = page.locator('a.nav-link[id^="tab-label-"]')
        n = labels.count()
        tab_ids = [labels.nth(i).get_attribute("id") for i in range(n)]
        tab_names = [labels.nth(i).inner_text().strip() for i in range(n)]
        if n == 0:
            tab_ids, tab_names = [None], [safe_title]
        # Survey pages ("Page 1..N" tabs gated behind required questions) keep their
        # step content in #surveyStepContent; steps are fetched server-side instead
        # of clicked (the gate is client-side only — /Project/LiveStep returns any step).
        is_survey = page.evaluate("() => !!document.getElementById('surveyStepContent')")
        print(f"{title}: PublicInput {'survey, ' + str(len(tab_ids)) + ' page(s)' if is_survey else 'page, ' + str(len(tab_ids)) + ' tab(s)'}")

        def goto_tab(tid):
            """Click a tab and wait until its content has replaced the previous tab's."""
            before = page.evaluate(
                "() => (document.querySelector('section.project-content')||{innerHTML:''}).innerHTML")
            active = page.evaluate(
                f"() => document.getElementById('{tid}').closest('li').classList.contains('active')")
            if active:
                return
            page.locator(f"#{tid}").click()
            deadline = time.time() + TAB_TIMEOUT_MS / 1000
            while time.time() < deadline:
                page.wait_for_timeout(300)
                now = page.evaluate(
                    "() => (document.querySelector('section.project-content')||{innerHTML:''}).innerHTML")
                busy = page.evaluate("() => window.jQuery ? jQuery.active : 0")
                if now != before and not busy:   # content swapped AND no request still in flight
                    return

        def goto_step(idx):
            """Load survey step idx into #surveyStepContent via /Project/LiveStep."""
            res = page.evaluate(LOAD_STEP_JS, idx)
            if res != "SUCCESS":
                print(f"  step {idx + 1}: LiveStep returned {res}")

        # Quick pass: text only, short settle, no screenshots — enough to decide
        # whether anything changed. Only a changed page pays for the full capture.
        texts = []
        for idx, tid in enumerate(tab_ids):
            if is_survey:
                goto_step(idx)
            elif tid is not None:
                goto_tab(tid)
            text = settled_text(page)
            if text is None:
                sys.exit("No section.project-content found — is this a PublicInput page?")
            texts.append(text)
        record = {"title": title, "url": url, "org": org, "exported": stamp, "width": width,
                  "tabs": [{"name": nm, "text": tx} for nm, tx in zip(tab_names, texts)]}

        prev_path, prev, diff_text, proceed = check_changes(record, out_dir, safe_title, out, force)
        if not proceed:
            browser.close()
            return None, prev, out_dir

        # Full pass: reload for a clean DOM, then visit every tab and snapshot it
        # with images settled and embeds/hero screenshotted.
        navigate(page)
        page.wait_for_timeout(SETTLE_MS * 2)
        for idx, (tid, name) in enumerate(zip(tab_ids, tab_names)):
            if is_survey:
                goto_step(idx)
            elif tid is not None:
                goto_tab(tid)
            settled_dom(page)                      # questions / comment widgets load after the tab swap
            page.wait_for_timeout(SETTLE_MS)       # then images, charts, embeds
            shots = screenshot_iframes(page, "section.project-content iframe")   # map/video embeds as seen on screen
            hero_shot = None
            if idx == 0 and not original_hero:
                hero = page.locator(".header-div")
                if hero.count() and hero.first.is_visible():
                    hero_shot = base64.b64encode(hero.first.screenshot(
                        type="jpeg", quality=HERO_JPEG_QUALITY, timeout=5000)).decode()
            got = page.evaluate(SNAP_JS, [shots, hero_shot])
            if got == -1:
                sys.exit("No section.project-content found — is this a PublicInput page?")
            if VERBOSE:
                print(f"  snapped [{idx+1}/{len(tab_ids)}] {name}" +
                      (f" ({len([s for s in shots if s])} embed(s) captured)" if shots else ""))
        # the record's text comes from the full pass too, so it matches the rendered pages
        record["tabs"] = [{"name": nm, "text": tx, "links": lk}
                          for nm, tx, lk in zip(tab_names, page.evaluate("() => window.__piTexts"),
                                                page.evaluate("() => window.__piLinks"))]

        # Re-diff against the FULL capture: the embedded changes.diff must describe
        # what capture.json actually stores. If the quick pass misread a tab (e.g. a
        # slow AJAX response) but the full capture matches the previous one, the
        # "change" was a mirage — skip the export unless --force.
        if prev:
            _, diff_text = diff_captures(prev, record)
            if not diff_text:
                print("Full capture matches the previous one after all — the quick "
                      "check misread a still-loading tab.")
                if not force:
                    print("Nothing written (use --force to export anyway).")
                    browser.close()
                    return None, prev, out_dir

        # Phase B: rebuild the page linearly per tab, settle, measure, print.
        # Normally one page sized to the content; a tab taller than MAX_PAGE_PX
        # is printed at that height and flows onto continuation pages.
        pdfs, page_labels, bookmarks = [], [], []   # bookmarks: (tab name, first page index)
        for idx, name in enumerate(tab_names):
            page.evaluate(BUILD_JS, idx)
            page.wait_for_timeout(800)
            buf, height_px, n_pages = print_page(page, width)
            pdfs.append(buf)
            bookmarks.append((name, len(page_labels)))
            page_labels += [name] if n_pages == 1 else [f"{name} ({k}/{n_pages})" for k in range(1, n_pages + 1)]
            if VERBOSE:
                print(f"  rendered [{idx+1}/{len(tab_names)}] {name}: {height_px}px" +
                      (f" → {n_pages} pages" if n_pages > 1 else ""))

        browser.close()
    result = write_pdf(pdfs, page_labels, bookmarks, record, prev_path, diff_text, title, stamp, url, out)
    return result, record, out_dir


def check_changes(record, out_dir, safe_title, out, force):
    """Compare with the previous capture. Returns (prev_path, prev, diff_text, proceed)."""
    prev_path, prev = previous_capture(out_dir, safe_title, out, record.get("url"))
    diff_text = ""
    if prev:
        summary, diff_text = diff_captures(prev, record)
        print(f"Changes since {prev_path.name}:")
        print("\n".join(summary))
        if diff_text and VERBOSE:
            lines = diff_text.splitlines()
            if len(lines) > MAX_DIFF_PRINT_LINES:
                print("\n".join(lines[:MAX_DIFF_PRINT_LINES]))
                print(f"… ({len(lines) - MAX_DIFF_PRINT_LINES} more lines — "
                      "full diff embedded in the new PDF as changes.diff)")
            else:
                print(diff_text)
        elif not diff_text and not force:
            print("No changes since previous capture — nothing written (use --force to export anyway)."
                  if VERBOSE else "  unchanged — nothing written")
            return prev_path, prev, diff_text, False
    else:
        print("No previous capture with embedded text found in", out_dir)
    return prev_path, prev, diff_text, True


SAFELINK_RE = re.compile(r"^https?://[^/]*safelinks\.protection\.outlook\.com/\?.*?\burl=([^&]+)", re.IGNORECASE)


def unwrap_safelink(href):
    """A link pasted from Outlook arrives wrapped in Microsoft's safelinks
    redirector (City staff do this: the Green Infrastructure page links EDIMS
    documents that way). The wrapper answers our fetches with HTTP 500; the real
    URL is its `url=` parameter."""
    m = SAFELINK_RE.match(href)
    return unquote(m.group(1)) if m else href


def documents_from(record):
    """{href: preferred filename} of the documents a capture links to: PublicInput's
    curated Documents list, plus any content link (not header/nav/footer) whose target
    has a document extension or looks like a download endpoint. The download step
    HEADs the latter and drops anything that isn't served as a document."""
    docs = {}
    for tab in record.get("tabs", []):
        for link in tab.get("links", []):
            href, text = unwrap_safelink(link["href"]), link.get("text", "")
            if "/Customer/File/Full/" in href:
                docs.setdefault(href, text)
            elif not link.get("chrome") and (DOC_EXT_RE.search(href) or DOC_HINT_RE.search(href)):
                docs.setdefault(href, None)      # keep the server's filename
    return docs


# Linked pages worth keeping as PDFs alongside a page's documents: PublicInput
# email newsletters (publicinput.com/Email/<id>), which project pages link as
# "Project Update: March 2025". The email body lives at /EmailHtml/<id>; the
# /Email/ page only wraps it in a share bar. An email never changes, so each
# is captured once and remembered in Attachments/.index.json like a file.
LINKED_PAGE_RE = re.compile(r"^https://publicinput\.com/Email/([A-Za-z0-9]+)/?$", re.IGNORECASE)
EMAIL_BODY_URL = "https://publicinput.com/EmailHtml/{id}"


def linked_pages_from(record):
    """{url: link text} of the linked pages a capture points at."""
    pages = {}
    for tab in record.get("tabs", []):
        for link in tab.get("links", []):
            href = unwrap_safelink(link["href"]).split("#")[0].split("?")[0]
            if LINKED_PAGE_RE.match(href) and not link.get("chrome"):
                pages.setdefault(href, link.get("text", ""))
    return pages


def capture_linked_pages(folder, pages, width=PAGE_WIDTH_PX):
    """Print each linked page once into folder/ (skipping URLs the index knows)."""
    todo = {u: t for u, t in pages.items() if u not in _load_index(folder)}
    print(f"Linked pages: {len(pages)}" + (f", {len(todo)} new" if todo else " (all captured before)"))
    if not todo:
        return
    folder.mkdir(parents=True, exist_ok=True)
    index = _load_index(folder)
    stamp = datetime.now().strftime("%Y-%m-%d %H%M")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for url, text in todo.items():
            page = browser.new_page(viewport={"width": width, "height": 1000}, device_scale_factor=2)
            page.emulate_media(media="screen")
            try:
                navigate(page, url)
                page.wait_for_timeout(SETTLE_MS)
                title = clean_title(page.title()) or text or url
                m = LINKED_PAGE_RE.match(url)
                navigate(page, EMAIL_BODY_URL.format(id=m.group(1)))   # the email body itself
                page.wait_for_timeout(SETTLE_MS * 2)
                page.evaluate(SCROLL_JS)
                page.wait_for_timeout(SETTLE_MS)
                body_text = page.evaluate(GENERIC_PREP_JS, None)
                buf, height_px, n_pages = print_page(page, width)
                name = re.sub(r"[^\w.\- ()]+", "_", title).strip("_ ")[:120] + ".pdf"
                dest = folder / name
                if dest.exists():
                    dest = folder / (name[:-4] + f" - {stamp}.pdf")
                rec = {"title": title, "url": url, "exported": stamp, "width": width,
                       "tabs": [{"name": title, "text": body_text}]}
                write_pdf([buf], [title], [(title, 0)], rec, None, "", title, stamp, url, dest)
                data = dest.read_bytes()
                index[url] = dict(name=dest.name, size=len(data), etag="", last_modified="",
                                  sha256=hashlib.sha256(data).hexdigest(), fetched=stamp, page=True)
            except Exception as e:
                _doc_failed(folder, url, e)
            finally:
                page.close()
        browser.close()
    _save_index(folder, index)


def sync_docs(out_dir, record, all_docs=False, verify=False):
    """Bring Attachments/ up to date with the documents a capture links to.
    Runs on every capture, changed or not: unchanged pages cost one HEAD per
    file; new or revised files are downloaded. Over the cap, nothing is fetched
    unless --all-docs. Returns the over-cap MB, or 0."""
    docs = documents_from(record)
    pages = linked_pages_from(record)
    if not docs and not pages:
        return 0
    over = 0
    if docs:
        print(f"Documents: {len(docs)} linked")
        over = fetch_files(out_dir / "Attachments", [(u, n) for u, n in docs.items()],
                           cap_mb=None if all_docs else DOC_CAP_MB, verify=verify)
    if pages:
        capture_linked_pages(out_dir / "Attachments", pages)
    return over


# ArcGIS Experience Builder apps (experience.arcgis.com/experience/<id>/page/<name>)
# are several pages behind one URL: the nav links to the others, each with its
# own /page/ path. They are captured as tabs of one PDF, like PublicInput's.
EXB_RE = re.compile(r"^(https://experience\.arcgis\.com/experience/[0-9a-f]+)/page/[^?#]*", re.IGNORECASE)
EXB_PAGES_JS = """
(base) => {
  const seen = new Set(), out = [];
  for (const a of document.querySelectorAll('a[href]')) {
    const href = a.href.split('#')[0].split('?')[0];
    if (!href.startsWith(base + '/page/') || seen.has(href)) continue;
    seen.add(href);
    out.push({name: a.textContent.trim().slice(0, 80) || decodeURIComponent(href.split('/page/')[1]), url: href});
  }
  return out;
}
"""


def subpages(page, url):
    """[(tab name, url)] — the current page first, then the app's other pages.
    Only Experience Builder apps have any; everything else is one page."""
    m = EXB_RE.match(url)
    if not m:
        return []
    pages = page.evaluate(EXB_PAGES_JS, m.group(1))
    cur = url.split("#")[0].split("?")[0].rstrip("/")
    ordered = [p for p in pages if p["url"].rstrip("/") == cur] + [p for p in pages if p["url"].rstrip("/") != cur]
    return [(p["name"], p["url"]) for p in ordered]


def archive_generic(page, browser, url, title, safe_title, stamp, out, out_dir, width, force, org):
    """Non-PublicInput page: expand, un-fix, print the live document as one page
    — or one page per sub-page for an Experience Builder app."""
    pages = subpages(page, url) or [(safe_title, None)]      # url None: already loaded, reload in place
    print(f"{title}: " + (f"{len(pages)} pages: " + ", ".join(n for n, _ in pages) if len(pages) > 1 else "single page"))
    tabs = []
    for i, (name, u) in enumerate(pages):
        if i:                                         # the first is already loaded
            navigate(page, u)
            page.wait_for_timeout(SETTLE_MS * 2)
        tabs.append({"name": name, "text": page.evaluate(GENERIC_PREP_JS, None)})
    record = {"title": title, "url": url, "org": org, "exported": stamp, "width": width, "tabs": tabs}
    prev_path, prev, diff_text, proceed = check_changes(record, out_dir, safe_title, out, force)
    if not proceed:
        browser.close()
        return (None, prev)          # unchanged: caller still syncs documents against the last record
    pdfs, labels, bookmarks = [], [], []
    for (name, u), tab in zip(pages, tabs):
        if u is None:
            navigate(page)                            # a clean DOM: the quick pass expanded things in place
        else:
            navigate(page, u)
        page.wait_for_timeout(SETTLE_MS * 2)
        page.evaluate(SCROLL_JS)                 # trigger lazy-loaded images (StoryMaps, Drupal, …)
        page.wait_for_timeout(SETTLE_MS)
        shots = screenshot_iframes(page, "iframe, canvas")   # must match GENERIC_PREP_JS's replacement selector
        tab["links"] = page.evaluate(LINKS_JS)
        tab["text"] = page.evaluate(GENERIC_PREP_JS, shots)
        page.wait_for_timeout(800)
        buf, height_px, n_pages = print_page(page, width)
        if VERBOSE:
            print(f"  rendered {name}: {height_px}px" + (f" → {n_pages} pages" if n_pages > 1 else ""))
        bookmarks.append((name, len(labels)))
        labels += [name] if n_pages == 1 else [f"{name} ({k}/{n_pages})" for k in range(1, n_pages + 1)]
        pdfs.append(buf)
    if prev:                                     # re-diff against what will actually be stored
        _, diff_text = diff_captures(prev, record)
        if not diff_text:
            print("Full capture matches the previous one after all — the quick "
                  "check caught the page mid-load.")
            if not force:
                print("Nothing written (use --force to export anyway).")
                browser.close()
                return (None, record)
    browser.close()
    return (write_pdf(pdfs, labels, bookmarks, record, prev_path, diff_text, title, stamp, url, out), record)


def write_pdf(pdfs, page_labels, bookmarks, record, prev_path, diff_text, title, stamp, url, out):
    merged = pikepdf.Pdf.new()
    for buf in pdfs:
        with pikepdf.open(io.BytesIO(buf)) as src:
            if hasattr(merged, "add_pages_from"):        # pikepdf >= 10.11: preserves link targets
                merged.add_pages_from(src)
            else:                                        # older pikepdf: same result for our link-free pages
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    merged.pages.extend(src.pages)
    dedupe_images(merged)

    # Name the pages: bookmarks + page labels (visible in Preview's sidebar)
    with merged.open_outline() as outline:
        for name, first_page in bookmarks:
            outline.root.append(pikepdf.OutlineItem(name, first_page))
    nums = pikepdf.Array()
    for i, name in enumerate(page_labels):
        nums.append(i)
        nums.append(pikepdf.Dictionary(P=pikepdf.String(name)))
    merged.Root.PageLabels = pikepdf.Dictionary(Nums=nums)

    with merged.open_metadata() as meta:
        meta["dc:title"] = f"{title} — exported {stamp}"
        meta["dc:source"] = url

    # Embed the capture record (and the diff, if any) inside the PDF
    merged.attachments["capture.json"] = pikepdf.AttachedFileSpec(
        merged, json.dumps(record, ensure_ascii=False, indent=1).encode("utf-8"),
        description="Per-tab plain text captured at export time (for change tracking)",
        mime_type="application/json")
    if diff_text:
        merged.attachments["changes.diff"] = pikepdf.AttachedFileSpec(
            merged, f"# vs {prev_path.name}\n\n{diff_text}\n".encode("utf-8"),
            description="Text changes since the previous capture", mime_type="text/plain")

    merged.save(out)
    print(f"Saved {out} ({out.stat().st_size/1e6:.1f} MB, {len(pdfs)} pages)")
    return out


def drive_download_url(url):
    """Google Drive viewer/preview/embed links -> direct-download URL, else None."""
    m = (re.search(r"drive\.google\.com/file/d/([\w-]+)", url)
         or re.search(r"drive\.google\.com/(?:open|uc)\?[^\"']*?id=([\w-]+)", url))
    return f"https://drive.google.com/uc?export=download&id={m.group(1)}" if m else None


def _load_index(folder):
    try:
        return json.loads((folder / ".index.json").read_text())
    except (OSError, ValueError):
        return {}


def _save_index(folder, index):
    (folder / ".index.json").write_text(json.dumps(index, indent=1, sort_keys=True) + "\n")


def _probe(req, url):
    """HEAD a URL -> dict(ok, status, size, etag, last_modified, type), never raises.
    A server that refuses HEAD (PublicInput's /Customer/File/Full/ answers 405,
    TxDOT's document store 403) is asked for the first byte instead: a 206
    carries the total size in Content-Range and the same headers a HEAD would."""
    try:
        r = req.head(url, timeout=30000)
        size = int(r.headers.get("content-length") or 0)
        if r.status in (403, 405, 501):
            r = req.get(url, headers={"Range": "bytes=0-0"}, timeout=30000)
            m = re.search(r"/(\d+)\s*$", r.headers.get("content-range", ""))
            size = int(m.group(1)) if m else int(r.headers.get("content-length") or 0)
        h = r.headers
        return dict(ok=r.ok, status=r.status, size=size,
                    etag=h.get("etag", ""), last_modified=h.get("last-modified", ""),
                    type=h.get("content-type", "").split(";")[0].strip().lower())
    except Exception as e:
        return dict(ok=False, status=str(e), size=0, etag="", last_modified="", type="")


def _unchanged(entry, probe):
    """Lazy check: ETag when both sides have one; else size + Last-Modified when
    both sides have them; else size alone."""
    if not entry or not probe["size"]:
        return False
    if entry.get("etag") and probe["etag"]:
        return entry["etag"] == probe["etag"]
    if entry.get("last_modified") and probe["last_modified"]:
        return entry["size"] == probe["size"] and entry["last_modified"] == probe["last_modified"]
    return entry["size"] == probe["size"]


def fetch_files(folder, items, cap_mb=None, verify=False):
    """Download linked documents into a capture folder. Items are URLs or
    (url, preferred_name) pairs. Keeps the server's filename (else the preferred
    name); an identical file already there is skipped, a different one with the
    same name gets a date stamp. Runs through Chromium's request stack so
    redirects, cookies and content-disposition behave like a browser download.
    Google Drive links (file/d/…, /preview embeds, open?id=…) are converted to
    direct downloads, including the are-you-sure page Drive serves for big files.

    Attachments/.index.json remembers, per URL, the file it became and the
    server's size / ETag / Last-Modified / our SHA-256. By default each URL is
    HEADed and skipped when those match (verify=False); verify=True downloads
    and hashes regardless. A file on disk with no index entry is adopted when
    its size matches (first run after this index was introduced). Extensionless
    candidates whose HEAD says they aren't a document are dropped. When the new
    downloads would exceed cap_mb, nothing is fetched and the total is returned;
    otherwise returns 0."""
    folder.mkdir(parents=True, exist_ok=True)
    index = _load_index(folder)
    stamp = datetime.now().strftime("%Y-%m-%d %H%M")
    over_cap = 0
    unchanged = 0            # quiet mode prints one count instead of a line per file
    with sync_playwright() as p:
        req = p.request.new_context(user_agent="Mozilla/5.0 (Macintosh) archive_page.py")
        plan = []          # (url, preferred, probe) still to download
        for item in items:
            url, preferred = item if isinstance(item, tuple) else (item, None)
            if drive_download_url(url):
                plan.append((url, preferred, None))       # Drive answers HEAD with HTML; just download
                continue
            probe = _probe(req, url)
            if not probe["ok"]:
                _doc_failed(folder, url, f"HTTP {probe['status']}")
                continue
            if not DOC_EXT_RE.search(url) and not probe["type"].startswith(DOC_TYPES):
                continue                                   # a download-looking link that serves HTML
            entry = index.get(url)
            if entry and (folder / entry["name"]).exists():
                if not verify and _unchanged(entry, probe):
                    unchanged += 1
                    if VERBOSE:
                        print(f"  unchanged: {entry['name']}")
                    continue
            elif not verify and probe["size"] and not entry:
                # legacy file (downloaded before the index existed) — adopt on size
                name = preferred and re.sub(r"[^\w.\- ()]+", "_", unquote(preferred)) \
                       or re.sub(r"[^\w.\- ()]+", "_", unquote(url.split("?")[0].rstrip("/").split("/")[-1]))
                dest = folder / name
                if dest.exists() and dest.stat().st_size == probe["size"]:
                    index[url] = dict(name=name, size=probe["size"], etag=probe["etag"],
                                      last_modified=probe["last_modified"], sha256="", fetched=stamp)
                    print(f"  adopted: {name}")
                    continue
            plan.append((url, preferred, probe))
        total_mb = sum((pr or {}).get("size", 0) for _, _, pr in plan) / 1e6
        if cap_mb is not None and total_mb > cap_mb:
            print(f"  {len(plan)} file(s), {total_mb:,.0f} MB — over the {cap_mb} MB cap; "
                  f"re-run with --all-docs to fetch them")
            over_cap = round(total_mb)
            plan = []
        for url, preferred, probe in plan:
            try:
                drive = drive_download_url(url)
                r = req.get(drive or url, timeout=120000)
                if drive and r.ok and "text/html" in r.headers.get("content-type", ""):
                    # big-file confirm page: replay its form (virus-scan bypass)
                    body = r.text()
                    action = re.search(r'action="([^"]+)"', body)
                    fields = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', body))
                    if action and fields:
                        r = req.get(action.group(1).replace("&amp;", "&"), params=fields, timeout=120000)
                if not r.ok:
                    _doc_failed(folder, url, f"HTTP {r.status}")
                    continue
                cd = r.headers.get("content-disposition", "")
                m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", cd)
                name = (m.group(1) if m else preferred
                        or url.split("?")[0].rstrip("/").split("/")[-1] or "download")
                name = re.sub(r"[^\w.\- ()]+", "_", unquote(name))
                data = r.body()
                digest = hashlib.sha256(data).hexdigest()
                dest = folder / name
                if dest.exists():
                    if hashlib.sha256(dest.read_bytes()).hexdigest() == digest:
                        unchanged += 1
                        if VERBOSE:
                            print(f"  unchanged: {name}" + (" (verified)" if verify else ""))
                        index[url] = dict(name=name, size=len(data), etag=r.headers.get("etag", ""),
                                          last_modified=r.headers.get("last-modified", ""),
                                          sha256=digest, fetched=index.get(url, {}).get("fetched", stamp))
                        continue
                    stem, dot, ext = name.rpartition(".")
                    dest = folder / (f"{stem} - {stamp}.{ext}" if dot else f"{name} - {stamp}")
                dest.write_bytes(data)
                index[url] = dict(name=dest.name, size=len(data), etag=r.headers.get("etag", ""),
                                  last_modified=r.headers.get("last-modified", ""), sha256=digest, fetched=stamp)
                print(f"  saved {dest.name} ({len(data)/1e6:.1f} MB) <- {url}")
            except Exception as e:
                _doc_failed(folder, url, e)
        req.dispose()
    if unchanged and not VERBOSE:
        print(f"  {unchanged} unchanged")
    _save_index(folder, index)
    return over_cap


def registry_pages(path=REGISTRY):
    """The rows of sources.csv this script owns: archive=yes and status=active
    (paused and retired rows are left alone; git remembers them)."""
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return [r for r in rows if r.get("archive") == "yes" and r.get("status") == "active"]


def check_interval(check):
    """A row's `check` cell as days between checks; unrecognised text = daily."""
    c = (check or "").strip().lower()
    if c in CHECK_INTERVALS:
        return CHECK_INTERVALS[c]
    m = CHECK_EVERY_RE.fullmatch(c)
    if m:
        return int(m.group(1)) * {"day": 1, "week": 7, "month": 30}[m.group(2).lower()]
    return 1


def in_expect_window(expect, today):
    """Is the page in its `expect` season? Uses the calendars repo's grammar
    (caltools/registry.py) when that repo is checked out next to this one, so
    there is one implementation; without it every page is treated as in
    season, which only means daily checks."""
    try:
        if str(CALENDARS_REPO) not in sys.path:
            sys.path.insert(0, str(CALENDARS_REPO))
        from caltools.registry import expected_now
    except ImportError:
        return True
    return expected_now(expect, today)


STAMP_RE = re.compile(r" - (\d{4}-\d{2}-\d{2} \d{4})\.pdf$")


def newest_capture(out_dir, safe_title):
    """(file name, local capture time) of the newest PDF in a page's folder."""
    for path in sorted(out_dir.glob(f"{safe_title} - *.pdf"), reverse=True):
        m = STAMP_RE.search(path.name)
        if m:
            return path.name, datetime.strptime(m.group(1), "%Y-%m-%d %H%M")
    return None, None


def utc_stamp(dt):
    """Local datetime -> the ISO-Z form status.json uses."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_captures(path=CAPTURES):
    try:
        doc = json.loads(path.read_text())
        return {"generated": doc.get("generated", ""), "sources": dict(doc.get("sources", {}))}
    except (OSError, ValueError):
        return {"generated": "", "sources": {}}


def run_batch(due_only, opts, registry_path=REGISTRY, captures_path=CAPTURES):
    """--all / --due: walk the registry's archive rows, capture what is due,
    and record per slug when the page was last checked and when its newest
    capture was taken. Only file names go into captures.json (it is public)."""
    pages = registry_pages(registry_path)
    captures = load_captures(captures_path)
    today = datetime.now().date()
    print(f"=== {datetime.now():%Y-%m-%d %H:%M} — {'due' if due_only else 'all'} archive pages "
          f"in {registry_path.name}: {len(pages)}\n")
    written, failed, skipped = [], [], []
    for r in pages:
        slug = r["slug"]
        entry = dict(captures["sources"].get(slug, {}))
        days = 1 if in_expect_window(r.get("expect", ""), today) else check_interval(r.get("check"))
        if due_only and entry.get("checked"):
            last = datetime.strptime(entry["checked"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            # two hours of grace so a daily job that fires a little early still counts
            if datetime.now(timezone.utc) - last < timedelta(days=days) - timedelta(hours=2):
                skipped.append(slug)
                continue
        print(f"[{slug}] {r['url']}")
        LAST.clear()
        checked_at = datetime.now()
        try:
            result = archive(r["url"], **opts)
        except Exception as e:
            failed.append((slug, e))
            print(f"FAILED {slug}: {e}\n")
            continue
        if result:
            written.append(result.name)
        entry["checked"] = utc_stamp(checked_at)
        if LAST:
            entry["folder"] = LAST["out_dir"].name        # page-title folder under the archive root
            name, when = newest_capture(LAST["out_dir"], LAST["safe_title"])
            if name:
                entry["captured"] = utc_stamp(when.astimezone())
                entry["file"] = name
        captures["sources"][slug] = entry
        print()
    captures["generated"] = utc_stamp(datetime.now())
    captures["sources"] = {k: captures["sources"][k] for k in sorted(captures["sources"])}
    captures_path.parent.mkdir(parents=True, exist_ok=True)
    captures_path.write_text(json.dumps(captures, indent=1) + "\n")
    # Object storage, when configured (R2_BUCKET etc. in the environment): every
    # capture and document goes up under a stable key, and captures.json gains
    # the public URL of each page's newest capture for the sources table.
    if os.environ.get("R2_BUCKET"):
        try:
            import r2sync
            r2sync.sync(ARCHIVE_ROOT, captures_path)
        except Exception as e:
            print(f"R2 sync failed (captures are safe locally): {e}")
    checked = len(pages) - len(skipped)
    print(f"Done: {len(written)} updated, {checked - len(written) - len(failed)} unchanged, "
          f"{len(failed)} failed, {len(skipped)} not due")
    for name in written:
        print("  updated:", name)
    for slug, e in failed:
        print("  failed: ", slug, "—", e)
    for url, mb in OVER_CAP:
        print(f"  documents skipped ({mb:,} MB over cap; use --all-docs):", url)
    if DOC_FAILURES:
        print(f"Documents not fetched: {len(DOC_FAILURES)}")
        for page, url, reason in DOC_FAILURES:
            print(f"  {page}: {url} — {reason}")
    print(f"Capture times written to {captures_path}")


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = [a for a in sys.argv[1:] if a.startswith("--")]
    width = PAGE_WIDTH_PX
    out_dir = None
    registry = REGISTRY
    for a in flags:
        if a.startswith("--width="):
            width = int(a.split("=", 1)[1])
        elif a.startswith("--out-dir="):
            out_dir = Path(a.split("=", 1)[1]).expanduser()
        elif a.startswith("--registry="):
            registry = Path(a.split("=", 1)[1]).expanduser()
    opts = dict(width=width, original_hero="--original-hero" in flags, force="--force" in flags,
                fetch_docs="--no-docs" not in flags, all_docs="--all-docs" in flags,
                verify="--verify" in flags)

    if "--fetch" in flags:
        if len(args) < 2:
            sys.exit('usage: archive_page.py --fetch "<page title>" URL [URL ...]')
        folder = (out_dir or ARCHIVE_ROOT) / args[0] / "Attachments"
        print(f"Fetching {len(args) - 1} file(s) into {folder}")
        fetch_files(folder, args[1:], cap_mb=None, verify=opts["verify"])   # explicit list: no cap
        return

    if "--sync" in flags:
        import r2sync
        r2sync.sync(ARCHIVE_ROOT, CALENDARS_REPO / "docs" / "captures.json",
                    dry_run="--dry-run" in flags, verify="--verify" in flags)
        return

    if "--all" in flags or "--due" in flags:
        global VERBOSE
        VERBOSE = "--verbose" in flags
        if not registry.exists():
            sys.exit(f"registry not found: {registry} (pass --registry=FILE)")
        # --out-dir in batch mode is the archive root; captures.json goes next to it too
        run_batch("--due" in flags, dict(opts, root=out_dir), registry,
                  (out_dir / "captures.json") if out_dir else CAPTURES)
        return

    archive(args[0], explicit_out=args[1] if len(args) > 1 else None, out_dir=out_dir, **opts)
    for url, mb in OVER_CAP:
        print(f"Documents skipped: {mb:,} MB is over the {DOC_CAP_MB} MB cap — re-run with --all-docs")


if __name__ == "__main__":
    main()
