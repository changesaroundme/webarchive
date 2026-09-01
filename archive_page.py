#!/usr/bin/env python3
"""Archive a civic webpage as a single PDF with the captured text embedded and
a change report against the previous capture.

Two kinds of page are handled:
  * PublicInput (Speak Up Austin) project pages — one content-sized PDF page per tab.
  * Any other page (e.g. austintexas.gov project pages) — one content-sized PDF page,
    with collapsed sections (accordions, <details>) expanded first.

Usage:
    python archive_page.py URL [output.pdf] [options]
    python archive_page.py --all [options]      # re-check every page already archived

Options:
    --width=PX          layout width (default 1800; keep >= 1200 or the sidebar collapses)
    --out-dir=DIR       where captures go (default: Archive vault, Tooling/Web Archive/<page title>/)
    --original-hero     keep the hero banner's source image (lossless, ~7 MB once)
                        instead of the default 2x JPEG screenshot (~0.8 MB)
    --force             write a PDF even when the text is identical to the previous capture
                        (default: unchanged pages are not re-exported)
    --all               batch mode: every page with a capture under the output root (its URL
                        is read from the newest PDF's capture.json) is re-checked in turn

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
import re
import sys
import time
import warnings
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright
import pikepdf

PAGE_WIDTH_PX = 1800          # layout width; keep >=1200 or the sidebar column collapses. ~1800 matches Ian's Safari exports
PX_PER_IN = 96
SETTLE_MS = 1800              # wait after tab content arrives (images, charts, embeds) — full capture
QUICK_SETTLE_MS = 300         # polling interval while waiting for a tab's text to stop changing (change-check pass)
TAB_TIMEOUT_MS = 12000
HERO_JPEG_QUALITY = 90        # hero banner is captured once at 2x (Retina) and shared by all pages
MAX_PAGE_PX = 0               # 0 = one page per tab regardless of height (Preview is fine with very tall pages).
                              # Set to e.g. 18000 to split taller tabs onto continuation pages (Acrobat caps pages at 200in = 19200px).
ARCHIVE_TOOLING = (Path.home() / "Library/Mobile Documents/iCloud~md~obsidian/Documents"
                   / "Archive - Changes Around Me/Tooling")
ARCHIVE_ROOT = ARCHIVE_TOOLING / "Web Archive"     # every page: <root>/<page title>/<title> - <stamp>.pdf
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
  for (const e of content.querySelectorAll('.collapse:not(.show)')) { e.classList.add('show', 'in'); e.style.height = 'auto'; }
  for (const t of content.querySelectorAll('.collapsed')) t.classList.remove('collapsed');
  for (const d of content.querySelectorAll('details')) d.open = true;
}
"""

# Change-check pass: just the tab's visible text (after expansion), or null.
TEXT_JS = "() => { const c = document.querySelector('section.project-content'); if (!c) return null; (" + EXPAND_JS.strip() + ")(c); return c.innerText; }"

# Generic pages: expand everything collapsed, pin fixed/sticky chrome into the
# flow, hide floating widgets. Returns the page text. Used for the change check
# and (with iframe screenshots swapped in) for the render.
GENERIC_PREP_JS = """
(iframeShots) => {
  const q = (s) => [...document.querySelectorAll(s)];
  q('.ckeditor-accordion-container dt').forEach(d => d.classList.add('active'));   // Drupal accordions (austintexas.gov)
  q('.ckeditor-accordion-container dd').forEach(d => d.style.display = 'block');
  (""" + EXPAND_JS.strip() + """)(document.body);
  for (const e of q('*')) { const s = getComputedStyle(e); if (s.position === 'fixed' || s.position === 'sticky') e.style.position = 'static'; }
  for (const e of q('[class*="userway"],.grecaptcha-badge,[class*="VIpgJd"],.asw-menu-btn,.asw-container')) e.style.display = 'none';
  if (iframeShots) q('iframe').forEach((f, i) => {
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
    agree and no 'Loading…' placeholder remains (capped at 2×SETTLE_MS). Static
    tabs finish in two reads; tabs still fetching comment widgets wait as long
    as they need, so the check sees the same state the full capture will store."""
    deadline = time.time() + 2 * SETTLE_MS / 1000
    last = None
    while True:
        page.wait_for_timeout(QUICK_SETTLE_MS)
        text = page.evaluate(TEXT_JS)
        if text is None:
            return None
        if text == last and not re.search(r"^Loading\b", text, re.M):
            return text
        if time.time() > deadline:
            return text
        last = text


def clean_title(title):
    """'Sir Swante Palm ... | Austin Parks | AustinTexas.gov' -> 'Sir Swante Palm ...';
    'Central City District Plan - PublicInput' -> 'Central City District Plan'."""
    t = title.split(" | ")[0]
    t = re.sub(r"\s*-\s*PublicInput$", "", t)
    return re.sub(r"[^\w\- ]+", "", t).strip() or "page"


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


def archive(url, explicit_out=None, out_dir=None, width=PAGE_WIDTH_PX,
            original_hero=False, force=False):
    """Capture one page. Returns the written path, or None when unchanged."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": 1000},
                                device_scale_factor=2)  # iframe screenshots at 2x so map embeds stay crisp
        page.emulate_media(media="screen")   # site's @media print styles hide the sidebar
        page.goto(url, wait_until="load", timeout=60000)
        page.wait_for_timeout(SETTLE_MS * 2)

        title = page.title()
        safe_title = clean_title(title)
        is_publicinput = page.locator("section.project-content").count() > 0
        org = detect_org(page, url, is_publicinput)
        stamp = datetime.now().strftime("%Y-%m-%d %H-%M")   # e.g. 2026-08-30 17-42
        if explicit_out:
            out = Path(explicit_out).expanduser()
            out_dir = out.parent
        else:
            out_dir = out_dir or (ARCHIVE_ROOT / safe_title)
            out = out_dir / f"{safe_title} - {stamp}.pdf"
        out_dir.mkdir(parents=True, exist_ok=True)

        if not is_publicinput:
            return archive_generic(page, browser, url, title, safe_title, stamp, out, out_dir, width, force, org)

        labels = page.locator('a.nav-link[id^="tab-label-"]')
        n = labels.count()
        tab_ids = [labels.nth(i).get_attribute("id") for i in range(n)]
        tab_names = [labels.nth(i).inner_text().strip() for i in range(n)]
        if n == 0:
            tab_ids, tab_names = [None], [safe_title]
        print(f"{title}: PublicInput page, {len(tab_ids)} tab(s)")

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
                if now != before:
                    return

        # Quick pass: text only, short settle, no screenshots — enough to decide
        # whether anything changed. Only a changed page pays for the full capture.
        texts = []
        for tid in tab_ids:
            if tid is not None:
                goto_tab(tid)
            text = settled_text(page)
            if text is None:
                sys.exit("No section.project-content found — is this a PublicInput page?")
            texts.append(text)
        record = {"title": title, "url": url, "org": org, "exported": stamp, "width": width,
                  "tabs": [{"name": nm, "text": tx} for nm, tx in zip(tab_names, texts)]}

        prev_path, diff_text, proceed = check_changes(record, out_dir, safe_title, out, force)
        if not proceed:
            browser.close()
            return None

        # Full pass: reload for a clean DOM, then visit every tab and snapshot it
        # with images settled and embeds/hero screenshotted.
        page.reload(wait_until="load", timeout=60000)
        page.wait_for_timeout(SETTLE_MS * 2)
        for idx, (tid, name) in enumerate(zip(tab_ids, tab_names)):
            if tid is not None:
                goto_tab(tid)
            page.wait_for_timeout(SETTLE_MS)
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
            print(f"  snapped [{idx+1}/{len(tab_ids)}] {name}" +
                  (f" ({len([s for s in shots if s])} embed(s) captured)" if shots else ""))
        # the record's text comes from the full pass too, so it matches the rendered pages
        record["tabs"] = [{"name": nm, "text": tx}
                          for nm, tx in zip(tab_names, page.evaluate("() => window.__piTexts"))]

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
            print(f"  rendered [{idx+1}/{len(tab_names)}] {name}: {height_px}px" +
                  (f" → {n_pages} pages" if n_pages > 1 else ""))

        browser.close()
    return write_pdf(pdfs, page_labels, bookmarks, record, prev_path, diff_text, title, stamp, url, out)


def check_changes(record, out_dir, safe_title, out, force):
    """Compare with the previous capture. Returns (prev_path, diff_text, proceed)."""
    prev_path, prev = previous_capture(out_dir, safe_title, out, record.get("url"))
    diff_text = ""
    if prev:
        summary, diff_text = diff_captures(prev, record)
        print(f"Changes since {prev_path.name}:")
        print("\n".join(summary))
        if diff_text:
            print(diff_text)
        elif not force:
            print("No changes since previous capture — nothing written (use --force to export anyway).")
            return prev_path, diff_text, False
    else:
        print("No previous capture with embedded text found in", out_dir)
    return prev_path, diff_text, True


def archive_generic(page, browser, url, title, safe_title, stamp, out, out_dir, width, force, org):
    """Non-PublicInput page: expand, un-fix, print the live document as one page."""
    print(f"{title}: single page")
    text = page.evaluate(GENERIC_PREP_JS, None)
    record = {"title": title, "url": url, "org": org, "exported": stamp, "width": width,
              "tabs": [{"name": safe_title, "text": text}]}
    prev_path, diff_text, proceed = check_changes(record, out_dir, safe_title, out, force)
    if not proceed:
        browser.close()
        return None
    page.reload(wait_until="load", timeout=60000)
    page.wait_for_timeout(SETTLE_MS * 2)
    shots = screenshot_iframes(page, "iframe")
    record["tabs"][0]["text"] = page.evaluate(GENERIC_PREP_JS, shots)
    page.wait_for_timeout(800)
    buf, height_px, n_pages = print_page(page, width)
    print(f"  rendered {safe_title}: {height_px}px" + (f" → {n_pages} pages" if n_pages > 1 else ""))
    browser.close()
    labels = [safe_title] if n_pages == 1 else [f"{safe_title} ({k}/{n_pages})" for k in range(1, n_pages + 1)]
    return write_pdf([buf], labels, [(safe_title, 0)], record, prev_path, diff_text, title, stamp, url, out)


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


def archived_urls(root):
    """URLs of every page already captured under root (newest PDF per folder,
    folders at any depth)."""
    urls = []
    for folder in sorted(p for p in root.rglob("*") if p.is_dir()):
        pdfs = sorted(folder.glob("*.pdf"))
        if not pdfs:
            continue
        try:
            with pikepdf.open(pdfs[-1]) as pdf:
                if "capture.json" in pdf.attachments:
                    urls.append(json.loads(pdf.attachments["capture.json"].get_file().read_bytes())["url"])
        except Exception as e:
            print(f"  (skipping {pdfs[-1].name}: {e})")
    return urls


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = [a for a in sys.argv[1:] if a.startswith("--")]
    width = PAGE_WIDTH_PX
    out_dir = None
    for a in flags:
        if a.startswith("--width="):
            width = int(a.split("=", 1)[1])
        elif a.startswith("--out-dir="):
            out_dir = Path(a.split("=", 1)[1]).expanduser()
    opts = dict(width=width, original_hero="--original-hero" in flags, force="--force" in flags)

    if "--all" in flags:
        root = out_dir or ARCHIVE_ROOT
        urls = archived_urls(root) if root.exists() else []
        print(f"Re-checking {len(urls)} archived page(s)\n")
        written, failed = [], []
        for url in urls:
            try:
                result = archive(url, **opts)
                if result:
                    written.append(result.name)
            except Exception as e:
                failed.append((url, e))
                print(f"FAILED {url}: {e}")
            print()
        print(f"Done: {len(written)} updated, {len(urls) - len(written) - len(failed)} unchanged, {len(failed)} failed")
        for name in written:
            print("  updated:", name)
        for url, e in failed:
            print("  failed: ", url, "—", e)
        return

    archive(args[0], explicit_out=args[1] if len(args) > 1 else None, out_dir=out_dir, **opts)


if __name__ == "__main__":
    main()
