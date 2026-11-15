#!/usr/bin/env python3
"""Checks for the linked-document downloader in archive_page.py (fetch_files).

    python3 tests/test_documents.py

Needs nothing installed: Playwright and pikepdf are stubbed, and the downloads
go to a pretend server defined below. Three parts:

  A  Widen viewer pages (fixtures/widen_viewer_*.html, real pages saved Nov 2026)
     resolve to the address of the file behind them.
  B  fetch_files against the pretend server: a viewer link saves the real PDF, a
     link that answers with a web page saves nothing and is reported, two links
     that share a file name do not multiply, a second run saves nothing, and a
     real revision is still caught.
  C  When the archive folder is reachable (CAM_ARCHIVE_ROOT, or the default
     path): no web page sits among the saved documents. Skipped otherwise.

Why these exist: until Nov 2026 the Widen inline viewer (/view/pdf/…) was saved
as if it were the document — 1,232 web pages named .pdf, a new one per link per
run, because the viewer's signed link differs on every request.
"""
import contextlib, io, json, re, sys, tempfile, types
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
for name in ("playwright", "playwright.sync_api", "pikepdf"):
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules["playwright.sync_api"].sync_playwright = None
sys.path.insert(0, str(HERE.parent))
import archive_page as ap                                                    # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""))


class Resp:
    def __init__(self, status=200, headers=None, body=b""):
        self.status, self.ok, self._body = status, 200 <= status < 300, body
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}

    def text(self):
        return self._body.decode("utf-8", "replace")

    def body(self):
        return self._body


class OnePage:
    """A request context that answers every GET with one page."""
    def __init__(self, body):
        self.page = body

    def get(self, url, **kw):
        return Resp(200, {"content-type": "text/html"}, self.page)


# ---------------------------------------------------------------- A: resolving viewers
WITH_DL = (HERE.parent / "fixtures" / "widen_viewer_with_download.html").read_bytes()
NO_DL = (HERE.parent / "fixtures" / "widen_viewer_no_download.html").read_bytes()
R = ap.WIDEN_SHARE_RE
check("A1 share page is resolved", R.match("https://austin.widen.net/s/lgcpfjjknv/some-name"))
check("A2 inline PDF viewer is resolved", R.match("https://austin.widen.net/view/pdf/mzg8hlurxt/TEP-DeanKeeton.pdf?t.download=true"))
check("A3 a direct file address is left alone", not R.match("https://austin.widen.net/content/mzg8hlurxt/original/TEP-DeanKeeton.pdf?download=true"))
check("A4 other hosts are left alone", not R.match("https://www.austintexas.gov/view/pdf/abc/x.pdf"))
got = ap.resolve_share_page(OnePage(WITH_DL), "https://austin.widen.net/view/pdf/mzg8hlurxt/TEP-DeanKeeton.pdf?t.download=true")
check("A5 viewer with a Download link -> the original file", got == "https://austin.widen.net/content/mzg8hlurxt/original/TEP-DeanKeeton.pdf?download=true", got)
got = ap.resolve_share_page(OnePage(NO_DL), "https://austin.widen.net/view/pdf/vedufhtwje/FY-2026-27-Proposed-Budget-Vol-I.pdf")
check("A6 viewer without one -> the PDF the viewer displays", bool(got) and got.startswith("https://previews.") and "/pdf/" in got, (got or "")[:80])
check("A7 a page with neither -> nothing", ap.resolve_share_page(OnePage(b"<!DOCTYPE html><html>gone</html>"), "https://austin.widen.net/view/pdf/x/y.pdf") is None)
check("A8 web pages are told from documents", ap._is_html(WITH_DL) and ap._is_html(b" \n<!DOCTYPE html><html>") and ap._is_html(b"<html style='x'>")
      and not ap._is_html(b"%PDF-1.7\n") and not ap._is_html(b"PK\x03\x04") and not ap._is_html(b"<?xml version='1.0'?><kml>"))

# ---------------------------------------------------------------- B: fetch_files end to end
VIEW = "https://austin.widen.net/view/pdf/abc123/Plan.pdf?t.download=true"          # viewer with a Download link
FILE = "https://austin.widen.net/content/xyz789/original/Plan.pdf?download=true"
VIEW2 = "https://austin.widen.net/view/pdf/nodl001/Budget-Vol-I.pdf"                # viewer without one
PREVIEW = "https://previews.us-east-1.widencdn.net/preview/1/assets/asset-view/uuid/pdf/bnVsbA==/Budget-Vol-I"
VIEW3 = "https://austin.widen.net/view/pdf/broken1/Gone.pdf?u=abc"                  # viewer with nothing behind it
SHARE = "https://austin.widen.net/s/sh4re1d/other-plan"
SHAREFILE = "https://austin.widen.net/content/sh4refile/original/Other-Plan.pdf?download=true"
DEAD = "https://www.example.org/docs/policy.pdf?v=2"                                # a .pdf link that serves a web page
EDIMS_A = "https://services.austintexas.gov/edims/document.cfm?id=1"                # two links, two files,
EDIMS_B = "https://services.austintexas.gov/edims/document.cfm?id=2"                # one file name
LINKS = [VIEW, SHARE, DEAD, EDIMS_A, EDIMS_B, VIEW2, VIEW3]


def pdf(text):
    return b"%PDF-1.7\n" + text * 200 + b"\n%%EOF"


VIEWER_PAGE = WITH_DL.replace(b"/content/mzg8hlurxt/original/TEP-DeanKeeton.pdf?download=true", b"/content/xyz789/original/Plan.pdf?download=true")
assert VIEWER_PAGE != WITH_DL
SHARE_PAGE = b'<!DOCTYPE html><html><body><a id="download" href="/content/sh4refile/original/Other-Plan.pdf?download=true">Download</a></body></html>'


class Server:
    """The pretend web: every page differs a little on each request, as the real ones do."""
    def __init__(self):
        self.files = {FILE: (pdf(b"plan v1 "), '"e1"'), SHAREFILE: (pdf(b"other "), '"s1"')}
        self.edims = {EDIMS_A: pdf(b"edims A "), EDIMS_B: pdf(b"edims B, a different file ")}
        self.preview = pdf(b"budget volume one ")
        self.n = 0

    def head(self, url, **kw):
        if url in self.files:
            body, etag = self.files[url]
            return Resp(200, {"content-type": "application/pdf", "content-length": str(len(body)), "etag": etag})
        if url in self.edims:
            return Resp(200, {"content-type": "application/pdf"})                   # no length, like EDIMS
        if url == DEAD:
            return Resp(200, {"content-type": "text/html", "content-length": "5000"})
        if url.startswith(PREVIEW):
            return Resp(403)                                                        # signed for GET only
        return Resp(404)

    def get(self, url, headers=None, params=None, timeout=None):
        self.n += 1
        html_page = {"content-type": "text/html"}
        if url == VIEW:
            return Resp(200, html_page, VIEWER_PAGE + b"<!-- %d -->" % self.n)
        if url == VIEW2:
            return Resp(200, html_page, b"<!DOCTYPE html><html><script>window.viewerPdfUrl = '%s?sig.expires=%d&sig=s%d';</script>"
                                        b'<button id="download">Download</button></html>' % (PREVIEW.encode(), 1790000000 + self.n, self.n))
        if url == VIEW3:
            return Resp(200, html_page, b"<!DOCTYPE html><html><body>This asset is no longer available</body></html>")
        if url == SHARE:
            return Resp(200, html_page, SHARE_PAGE)
        if url == DEAD:
            return Resp(200, html_page, b" <!DOCTYPE html><html><body>Page not found %d</body></html>" % self.n)
        if url.startswith(PREVIEW):
            if headers and headers.get("Range"):
                return Resp(206, {"content-type": "application/pdf", "content-range": "bytes 0-0/%d" % len(self.preview), "etag": '"p1"'}, self.preview[:1])
            return Resp(200, {"content-type": "application/pdf", "etag": '"p1"'}, self.preview)
        if url in self.files:
            body, etag = self.files[url]
            name = url.split("?")[0].rsplit("/", 1)[1]
            return Resp(200, {"content-type": "application/pdf", "etag": etag, "content-disposition": f'attachment; filename="{name}"'}, body)
        if url in self.edims:
            return Resp(200, {"content-type": "application/pdf", "content-disposition": 'inline; filename="Backup.PDF"'}, self.edims[url])
        return Resp(404)

    def dispose(self):
        pass


server = Server()


class FakePlaywright:
    def __enter__(self):
        return types.SimpleNamespace(request=types.SimpleNamespace(new_context=lambda **kw: server))

    def __exit__(self, *exc):
        return False


ap.sync_playwright = FakePlaywright


def run(folder):
    """One fetch_files pass -> (lines that say 'saved', file names on disk, failures)."""
    del ap.DOC_FAILURES[:]
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        ap.fetch_files(folder, LINKS)
    saved = [l for l in out.getvalue().splitlines() if l.strip().startswith("saved ")]
    return saved, sorted(p.name for p in folder.iterdir() if not p.name.startswith(".")), list(ap.DOC_FAILURES)


with tempfile.TemporaryDirectory() as tmp:
    folder = Path(tmp) / "Some Page" / "Attachments"
    saved, files, failed = run(folder)
    real = lambda name: (folder / name).exists() and (folder / name).read_bytes().startswith(b"%PDF")
    check("B1 viewer link: the real PDF is saved under its own name", real("Plan.pdf"))
    check("B2 viewer without a Download link: the PDF it displays is saved, extension kept", real("Budget-Vol-I.pdf"))
    check("B3 share page: still works", real("Other-Plan.pdf"))
    check("B4 a link that serves a web page: nothing saved, reported", not any("policy" in f for f in files)
          and any(u == DEAD and "web page" in why for _, u, why in failed))
    check("B5 a viewer with nothing behind it: reported", any(u == VIEW3 for _, u, _ in failed) and not any("Gone" in f for f in files))
    check("B6 two links, one file name: both files kept, one under a stamped name", len([f for f in files if f.startswith("Backup")]) == 2)
    check("B7 no web page was written to disk", not any(ap._is_html((folder / f).read_bytes()) for f in files), f"{len(files)} files")
    index = json.loads((folder / ".index.json").read_text())
    check("B8 the index is keyed by the link on the page", index.get(VIEW, {}).get("name") == "Plan.pdf"
          and index.get(VIEW2, {}).get("name") == "Budget-Vol-I.pdf" and DEAD not in index and VIEW3 not in index)
    for n in (2, 3):
        saved_again, files_again, _ = run(folder)
        check(f"B9 run {n} saves nothing new", saved_again == [] and files_again == files, f"{len(files_again)} files, as before")
    server.files[FILE] = (pdf(b"plan v2, revised "), '"e2"')
    saved_rev, files_rev, _ = run(folder)
    check("B10 a real revision is caught and saved beside the old file", len(saved_rev) == 1 and len(files_rev) == len(files) + 1
          and any(re.match(r"Plan - \d{4}-\d\d-\d\d \d{4}\.pdf$", f) for f in files_rev))
    saved_after, files_after, _ = run(folder)
    check("B11 and the run after that saves nothing", saved_after == [] and files_after == files_rev)

# ---------------------------------------------------------------- C: the archive itself
root = ap.ARCHIVE_ROOT
if root.is_dir():
    docs = pages = 0
    offenders = []
    for folder in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith((".", "_"))):
        att = folder / "Attachments"
        for doc in sorted(att.iterdir()) if att.is_dir() else []:
            if doc.is_file() and not doc.name.startswith("."):
                docs += 1
                with doc.open("rb") as fh:
                    if ap._is_html(fh.read(1024)):
                        pages += 1
                        offenders.append(f"{folder.name}/{doc.name}")
    check("C1 no web page among the archive's saved documents", pages == 0, f"{docs} documents checked" + (f"; first: {offenders[0]}" if offenders else ""))
else:
    print(f"SKIP C (archive folder not reachable: {root})")

print(f"\n{sum(results)} of {len(results)} checks passed")
sys.exit(0 if all(results) else 1)
