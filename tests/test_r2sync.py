#!/usr/bin/env python3
"""Checks for r2sync.py: what a sync prints, and how it stops when the bucket is out of reach.

    python3 tests/test_r2sync.py

Needs nothing installed and touches no network: the bucket is a dictionary in
memory, and the archive is a temporary folder built here (one live page, and a
`_to_delete/` holding 600 files of that page plus 30 of another).

Why these exist: in Nov 2026 a prune of 1,250 objects printed one line per key,
and a dropped connection printed a 100-line traceback that did not say whether
anything had been deleted.
"""
import contextlib, io, json, os, sys, tempfile
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import r2sync                                                                # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""))


class BucketDown(Exception):
    """Stands in for botocore's EndpointConnectionError."""


BucketDown.__module__ = "botocore.exceptions"


class FakeS3:
    def __init__(self, objects):
        self.objects = dict(objects)          # key -> size
        self.fail_listing = False
        self.fail_delete_after = None         # number of delete requests that succeed before the connection drops
        self.delete_calls = 0

    def get_paginator(self, name):
        s3 = self

        class Pages:
            def paginate(self, Bucket):
                if s3.fail_listing:
                    raise BucketDown('Could not connect to the endpoint URL: "https://example.r2.cloudflarestorage.com/cam-archive"')
                yield {"Contents": [{"Key": k, "Size": v} for k, v in sorted(s3.objects.items())]}
        return Pages()

    def upload_file(self, path, bucket, key, ExtraArgs=None):
        self.objects[key] = os.path.getsize(path)

    def delete_objects(self, Bucket, Delete):
        if self.fail_delete_after is not None and self.delete_calls >= self.fail_delete_after:
            raise BucketDown("Connection was closed before we received a valid response")
        self.delete_calls += 1
        for obj in Delete["Objects"]:
            self.objects.pop(obj["Key"], None)


def build(tmp: Path):
    """An archive root, its captures.json, and the bucket as it stood before the files were moved to _to_delete/."""
    root = tmp / "Web Archive"
    cal = tmp / "calendars" / "docs"
    cal.mkdir(parents=True)
    captures = {"sources": {"coa-big-report": {"folder": "Big Report"}, "coa-small-plan": {"folder": "Small Plan"}}}
    (cal / "captures.json").write_text(json.dumps(captures))
    bucket = {}

    def put(folder: Path, name: str, size: int, key: str | None):
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(b"x" * size)
        if key:
            bucket[key] = size

    for page, slug in (("Big Report", "coa-big-report"), ("Small Plan", "coa-small-plan")):
        put(root / page, f"{page} - 2026-10-01 0630.pdf", 100, f"{slug}/2026-10-01-0630.pdf")
        bucket[f"{slug}/latest.pdf"] = 100
    live = root / "Big Report" / "Attachments"
    put(live, "Kept.pdf", 50, "coa-big-report/files/2026-10-01-0630/Kept.pdf")
    put(live, "New.pdf", 70, None)                                          # not uploaded yet
    put(live, "Newer.pdf", 80, None)
    (live / ".index.json").write_text(json.dumps({
        "u1": {"name": "Kept.pdf", "fetched": "2026-10-01 0630"}, "u2": {"name": "New.pdf", "fetched": "2026-11-14 2210"},
        "u3": {"name": "Newer.pdf", "fetched": "2026-11-15 0700"}}))
    for page, slug, n in (("Big Report", "coa-big-report", 600), ("Small Plan", "coa-small-plan", 30)):
        trash = root / "_to_delete" / page / "Attachments"
        for i in range(n):
            day, minute = 1 + i // 60, i % 60
            put(trash, f"Viewer - 2026-10-{day:02d} 06{minute:02d}.pdf", 10, f"{slug}/files/2026-10-{day:02d}-06{minute:02d}/Viewer.pdf")
    (cal / "archive.json").write_text(json.dumps({"sources": {"coa-big-report": {"latest_key": "coa-big-report/2026-10-01-0630.pdf"},
                                                                 "coa-small-plan": {"latest_key": "coa-small-plan/2026-10-01-0630.pdf"}}}))
    return root, cal / "captures.json", bucket


def run(s3, root, captures, **kw):
    """One sync -> (printed lines, the SyncStopped message or None)."""
    r2sync._client = lambda: s3
    out, stopped = io.StringIO(), None
    with contextlib.redirect_stdout(out):
        try:
            r2sync.sync(root, captures, **kw)
        except r2sync.SyncStopped as e:
            stopped = str(e)
    return out.getvalue().splitlines(), stopped


os.environ.update(R2_BUCKET="cam-archive", R2_PUBLIC_URL="https://archive.example.org")
JUNK = lambda bucket: sum(1 for k in bucket if k.endswith("/Viewer.pdf"))

with tempfile.TemporaryDirectory() as tmp:
    root, captures, bucket = build(Path(tmp))
    archive_json = captures.parent / "archive.json"
    before_archive = archive_json.read_text()

    # ---- 1: dry run
    s3 = FakeS3(bucket)
    lines, stopped = run(s3, root, captures, dry_run=True, prune_deleted=True)
    text = "\n".join(lines)
    check("1a dry run changes nothing", s3.objects == bucket and archive_json.read_text() == before_archive and stopped is None)
    check("1b says what it found before doing anything", lines[0].startswith("R2: listing the bucket") and "635 object(s) in the bucket" in lines[1]
          and "2 to upload" in lines[1], lines[1])
    check("1c uploads are numbered", any("[1/2] would upload" in l for l in lines) and any("[2/2] would upload" in l for l in lines))
    check("1d deletions shown as a count per page, largest first", "would delete 630 object(s) from 2 page(s):" in text
          and text.index("600  coa-big-report") < text.index("30  coa-small-plan"))
    check("1e not one line per key", len(lines) < 20, f"{len(lines)} lines for 630 deletions")
    check("1f summary lines unchanged", "  _to_delete: 630 bucket object(s) to delete" in lines
          and any(l.startswith("R2 sync: 2 to upload, 3 already there") for l in lines))
    lines_v, _ = run(FakeS3(bucket), root, captures, dry_run=True, prune_deleted=True, verbose=True)
    check("1g --verbose lists every key", sum(1 for l in lines_v if l.startswith("  would delete coa-")) == 630)

    # ---- 2: the bucket cannot be reached at all
    s3 = FakeS3(bucket); s3.fail_listing = True
    lines, stopped = run(s3, root, captures, prune_deleted=True)
    check("2a stops with a message, not a traceback", stopped is not None and stopped.startswith("R2 sync stopped while listing the bucket: could not reach the bucket"))
    check("2b says nothing was changed, and to run it again", "Nothing was changed in the bucket." in (stopped or "") and "run the same command again" in (stopped or ""),
          f"{len((stopped or '').splitlines())} lines")
    check("2c and nothing was changed", s3.objects == bucket and archive_json.read_text() == before_archive)

    # ---- 3: the connection drops part-way through the deletions
    s3 = FakeS3(bucket); s3.fail_delete_after = 1
    lines, stopped = run(s3, root, captures, prune_deleted=True)
    check("3a uploads done first, numbered", any("[1/2] uploaded" in l for l in lines) and any("[2/2] uploaded" in l for l in lines))
    check("3b progress shown before the drop", "  deleted 250 of 630" in lines)
    check("3c message says how far it got", stopped is not None and "while deleting from the bucket" in stopped
          and "uploaded 2 of 2, deleted 250 of 630" in stopped and "The rest is untouched." in stopped, (stopped or "").splitlines()[1] if stopped else "")
    check("3d the bucket matches the message", JUNK(s3.objects) == 380, f"{JUNK(s3.objects)} of 630 junk objects left")
    check("3e archive.json not rewritten by a stopped run", archive_json.read_text() == before_archive)

    # ---- 4: running it again finishes the job
    s3.fail_delete_after = None
    lines, stopped = run(s3, root, captures, prune_deleted=True)
    text = "\n".join(lines)
    check("4a second run completes", stopped is None and JUNK(s3.objects) == 0)
    check("4b it deletes only what is left, with a running total", "deleting 380 object(s) from 2 page(s):" in text
          and "  deleted 250 of 380" in lines and "  deleted 380 of 380" in lines)
    check("4c already-deleted files are summarised, not listed", any("250 file(s) in _to_delete/ are not in the bucket" in l for l in lines))
    check("4d final lines unchanged", "  _to_delete: 380 bucket object(s) deleted — _to_delete/ can now be emptied" in lines
          and any(l.startswith("R2 sync: 0 uploaded, 5 already there") for l in lines))
    live_keys = {"coa-big-report/2026-10-01-0630.pdf", "coa-small-plan/2026-10-01-0630.pdf", "coa-big-report/latest.pdf", "coa-small-plan/latest.pdf",
                 "coa-big-report/files/2026-10-01-0630/Kept.pdf", "coa-big-report/files/2026-11-14-2210/New.pdf", "coa-big-report/files/2026-11-15-0700/Newer.pdf"}
    check("4e every live object is still there, and nothing else", set(s3.objects) == live_keys, f"{len(s3.objects)} objects")
    listed = json.loads(archive_json.read_text())["sources"]
    check("4f archive.json rewritten from the live files", [f["name"] for f in listed["coa-big-report"]["files"]] == ["Newer.pdf", "New.pdf", "Kept.pdf"]
          and listed["coa-small-plan"]["files"] == [])

    # ---- 5: a small deletion stays quiet; other errors keep their traceback
    lines, stopped = run(s3, root, captures, prune_deleted=True)
    check("5a nothing left to do: says so in the usual lines", stopped is None and "  _to_delete: 0 bucket object(s) deleted — _to_delete/ can now be emptied" in lines)
    r2sync.plan = lambda *a, **k: (_ for _ in ()).throw(ValueError("a bug, not the network"))
    try:
        run(s3, root, captures)
        check("5b an error that is not the bucket's is not swallowed", False)
    except ValueError:
        check("5b an error that is not the bucket's is not swallowed", True)

print(f"\n{sum(results)} of {len(results)} checks passed")
sys.exit(0 if all(results) else 1)
