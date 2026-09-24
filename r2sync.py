#!/usr/bin/env python3
"""Mirror the web archive into Cloudflare R2 (or any S3-compatible bucket).

    python r2sync.py [--dry-run] [--verify] [--prune-deleted] [--purge-old-keys]

Every capture PDF and every downloaded document goes up once, under a key
that never changes and is never overwritten — the bucket is an append-only
history. The one exception is each page's `latest.pdf`, a copy of its newest
capture kept under a stable address for links that must survive the page
itself disappearing. Keys:

    <slug>/latest.pdf                         the newest capture (overwritten as captures arrive)
    <slug>/2026-09-01-1652.pdf                every capture, by its local capture time
    <slug>/files/2026-09-10-0900/<file name>  a document, by the time it was fetched, name as the site served it
    _unlisted/<folder name>/...               a folder with no registry row

The document stamp comes from Attachments/.index.json (`fetched`); a file the
index does not know is stamped with its modification time. The local files
keep their own names — this mapping lives only here.

Configuration is environment only (never in the repo; run.sh passes it from
~/.config/cam-webarchive/r2.env):
    R2_ENDPOINT     https://<account id>.r2.cloudflarestorage.com
    R2_BUCKET       bucket name
    R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY   an API token scoped to that bucket
    R2_PUBLIC_URL   how the bucket is reachable in a browser, e.g.
                    https://archive.changesaroundme.com

After a sync two files next to captures.json describe the bucket for the
calendars build: captures.json gains each page's `url` (its latest.pdf), and
docs/archive.json lists every capture and document with its public URL (and,
per capture, how many text lines it changed against the one before — for
information only), from
which the build renders the vault's Archive page and orgpages.py fills the
organisation pages.

Deleting: the sync itself never deletes. A capture or document taken out of
the archive goes into the archive root's `_to_delete/` folder (by page
folder, or loose); `--prune-deleted` then removes the bucket objects those
files were uploaded as — and a page's latest.pdf when nothing of it is left —
after which `_to_delete/` can be emptied in Finder. Only keys that a file in
`_to_delete/` maps to are ever removed, and never one a live file also maps
to, so a wrong archive path cannot empty the bucket. `--purge-old-keys` deletes
objects under the first key scheme (Sep 2026: `<stamp> - <title>.pdf` and
`attachments/`) — a one-off after the re-key.
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

STAMP_RE = re.compile(r"^(.*?) - (\d{4}-\d{2}-\d{2}) (\d{4})\.pdf$")
DOC_STAMP_RE = re.compile(r"^(.*) - (\d{4}-\d{2}-\d{2}) (\d{4})(\.[^.]+)?$")
OLD_SCHEME_RE = re.compile(r"(^|/)\d{4}-\d{2}-\d{2} \d{4} - |/attachments/")
CONTENT_TYPES = {".pdf": "application/pdf", ".kmz": "application/vnd.google-earth.kmz",
                 ".kml": "application/vnd.google-earth.kml+xml", ".csv": "text/csv"}


def _client():
    import boto3
    from botocore.config import Config
    missing = [k for k in ("R2_ENDPOINT", "R2_BUCKET", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")
               if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"R2 not configured: missing {', '.join(missing)}")
    return boto3.client(
        "s3", endpoint_url=os.environ["R2_ENDPOINT"], region_name="auto",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        config=Config(signature_version="s3v4", retries={"max_attempts": 5}))


def existing_keys(s3, bucket: str) -> dict[str, int]:
    """key -> size for everything in the bucket (one listing per sync)."""
    keys: dict[str, int] = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        for obj in page.get("Contents", []):
            keys[obj["Key"]] = obj["Size"]
    return keys


def _fs_stamp(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d-%H%M")


def _content_type(path: Path) -> str:
    return CONTENT_TYPES.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def plan(root: Path, captures: dict) -> list[dict]:
    """One entry per local capture / document: {path, key, slug, kind, stamp, name}."""
    folder_slug = {v.get("folder"): slug for slug, v in captures.get("sources", {}).items() if v.get("folder")}
    out: list[dict] = []
    for folder in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith((".", "_"))):
        slug = folder_slug.get(folder.name) or f"_unlisted/{folder.name}"
        for pdf in sorted(folder.glob("*.pdf")):
            m = STAMP_RE.match(pdf.name)
            if not m:
                continue                                    # not one of ours
            stamp = f"{m.group(2)}-{m.group(3)}"
            out.append(dict(path=pdf, key=f"{slug}/{stamp}.pdf", slug=slug, kind="capture", stamp=stamp, name=pdf.name))
        att = folder / "Attachments"
        if not att.is_dir():
            continue
        index: dict = {}
        try:
            index = json.loads((att / ".index.json").read_text())
        except (OSError, ValueError):
            pass
        fetched = {v["name"]: (v.get("fetched") or "").replace(" ", "-") for v in index.values() if v.get("name")}
        for doc in sorted(p for p in att.iterdir() if p.is_file() and not p.name.startswith(".")):
            m = DOC_STAMP_RE.match(doc.name)
            if m:                                           # a later version already carries its stamp
                stamp, name = f"{m.group(2)}-{m.group(3)}", m.group(1) + (m.group(4) or "")
            else:
                stamp, name = fetched.get(doc.name) or _fs_stamp(doc), doc.name
            out.append(dict(path=doc, key=f"{slug}/files/{stamp}/{name}", slug=slug, kind="file", stamp=stamp, name=name))
    return out


def deleted_keys(root: Path, captures: dict) -> set[str]:
    """The bucket keys of the files in <root>/_to_delete/: page folders are
    mapped like live ones (plan), loose capture PDFs by their title's page
    folder."""
    trash = root / "_to_delete"
    if not trash.is_dir():
        return set()
    keys = {item["key"] for item in plan(trash, captures)}
    folder_slug = {v.get("folder"): slug for slug, v in captures.get("sources", {}).items() if v.get("folder")}
    for pdf in trash.glob("*.pdf"):
        m = STAMP_RE.match(pdf.name)
        if m:
            folder = m.group(1)
            keys.add(f"{folder_slug.get(folder) or '_unlisted/' + folder}/{m.group(2)}-{m.group(3)}.pdf")
    return keys


def _delete(s3, bucket: str, keys: list[str], dry_run: bool) -> None:
    for i in range(0, len(keys), 1000):
        chunk = keys[i:i + 1000]
        if dry_run:
            for k in chunk:
                print(f"  would delete {k}")
        else:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in chunk], "Quiet": True})
            for k in chunk:
                print(f"  deleted {k}")


def changed_lines(pdf: Path) -> int | None:
    """How much a capture differs from the one before it: the number of added
    and removed text lines in the changes.diff the capture carries. None for a
    first capture (no diff) or a file that cannot be read. Informational:
    recorded in archive.json so the size of each change can be seen later."""
    try:
        import pikepdf
        with pikepdf.open(pdf) as doc:
            if "changes.diff" not in doc.attachments:
                return None
            text = doc.attachments["changes.diff"].get_file().read_bytes().decode("utf-8", "replace")
    except Exception:
        return None
    # The diff names the capture it was taken against ("# vs <file>"). When
    # that capture has since been removed (a defective one moved to
    # _to_delete/), this one is the page's baseline now, not a revision of it.
    first = text.split("\n", 1)[0]
    if first.startswith("# vs ") and not (pdf.parent / first[5:].strip()).exists():
        return None
    return sum(1 for l in text.splitlines()
               if l[:1] in "+-" and not l.startswith(("+++", "---")) and l[1:].strip())


def public_url(key: str) -> str:
    base = os.environ.get("R2_PUBLIC_URL", "").rstrip("/")
    return f"{base}/{quote(key)}" if base else ""


def _put(s3, bucket, path: Path, key: str, dry_run: bool, verb: str = "upload") -> None:
    size = path.stat().st_size
    if dry_run:
        print(f"  would {verb} {key} ({size/1e6:.1f} MB)")
    else:
        s3.upload_file(str(path), bucket, key, ExtraArgs={"ContentType": _content_type(path)})
        print(f"  {verb}ed {key} ({size/1e6:.1f} MB)")


def sync(root: Path, captures_path: Path, dry_run: bool = False, verify: bool = False,
         purge_old: bool = False, prune_deleted: bool = False) -> None:
    captures = json.loads(captures_path.read_text()) if captures_path.exists() else {"sources": {}}
    archive_path = captures_path.parent / "archive.json"
    try:
        prior_latest = {s: v.get("latest_key") for s, v in json.loads(archive_path.read_text()).get("sources", {}).items()}
    except (OSError, ValueError, AttributeError):
        prior_latest = {}
    bucket = os.environ["R2_BUCKET"]
    s3 = _client()
    have = existing_keys(s3, bucket)
    todo = plan(root, captures)

    uploaded, skipped, mismatched = 0, 0, []
    for item in todo:
        key, path = item["key"], item["path"]
        if key in have:
            if verify and have[key] != path.stat().st_size:
                mismatched.append((key, have[key], path.stat().st_size))
            skipped += 1
            continue
        _put(s3, bucket, path, key, dry_run)
        uploaded += 1

    # latest.pdf per page: re-upload whenever the newest capture is not the one
    # the bucket's copy was made from (recorded in archive.json) or the sizes differ.
    newest: dict[str, dict] = {}
    for item in todo:
        if item["kind"] == "capture" and (item["slug"] not in newest or item["stamp"] > newest[item["slug"]]["stamp"]):
            newest[item["slug"]] = item
    refreshed = 0
    for slug, item in newest.items():
        latest_key = f"{slug}/latest.pdf"
        if prior_latest.get(slug) != item["key"] or have.get(latest_key) != item["path"].stat().st_size:
            _put(s3, bucket, item["path"], latest_key, dry_run, verb="refresh")
            refreshed += 1

    if prune_deleted:
        live = {item["key"] for item in todo}
        doomed = deleted_keys(root, captures) - live
        live_slugs = {item["slug"] for item in todo if item["kind"] == "capture"}
        slug_of = lambda k: k.split("/files/")[0] if "/files/" in k else k.rsplit("/", 1)[0]
        doomed |= {f"{slug_of(k)}/latest.pdf" for k in doomed if slug_of(k) not in live_slugs}
        gone = sorted(k for k in doomed if k in have)
        _delete(s3, bucket, gone, dry_run)
        absent = sorted(k for k in doomed if k not in have and not k.endswith("/latest.pdf"))
        for k in absent:
            print(f"  (not in the bucket: {k} — never uploaded, nothing to delete)")
        print(f"  _to_delete: {len(gone)} bucket object(s) {'to delete' if dry_run else 'deleted'}"
              + ("" if dry_run else " — _to_delete/ can now be emptied"))

    if purge_old:
        old = [k for k in have if OLD_SCHEME_RE.search(k)]
        _delete(s3, bucket, old, dry_run)
        print(f"  old-scheme keys {'to delete' if dry_run else 'deleted'}: {len(old)}")

    if not dry_run:
        # archive.json: every capture and document with its public URL, and
        # captures.json: each page's stable latest.pdf link.
        sources: dict[str, dict] = {}
        for item in todo:
            entry = sources.setdefault(item["slug"], {"captures": [], "files": []})
            rec = {"stamp": item["stamp"], "url": public_url(item["key"]), "size": item["path"].stat().st_size}
            if item["kind"] == "file":
                rec["name"] = item["name"]
            else:
                n = changed_lines(item["path"])
                if n is not None:
                    rec["changed_lines"] = n
            entry["captures" if item["kind"] == "capture" else "files"].append(rec)
        for slug, item in newest.items():
            sources.setdefault(slug, {"captures": [], "files": []})
            sources[slug]["latest_key"] = item["key"]
            sources[slug]["latest"] = public_url(f"{slug}/latest.pdf")
        for entry in sources.values():
            entry["captures"].sort(key=lambda r: r["stamp"], reverse=True)
            entry["files"].sort(key=lambda r: (r["stamp"], r["name"]), reverse=True)
        stamp_now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        archive_path.write_text(json.dumps({"generated": stamp_now, "public_url": os.environ.get("R2_PUBLIC_URL", ""),
                                            "sources": dict(sorted(sources.items()))}, indent=1) + "\n")
        for slug, entry in captures.get("sources", {}).items():
            if slug in newest and os.environ.get("R2_PUBLIC_URL"):
                entry["url"] = public_url(f"{slug}/latest.pdf")
        captures_path.write_text(json.dumps(captures, indent=1) + "\n")

    print(f"R2 sync: {uploaded} {'to upload' if dry_run else 'uploaded'}, {skipped} already there, "
          f"{refreshed} latest.pdf {'to refresh' if dry_run else 'refreshed'}"
          + (f", {len(mismatched)} size mismatch(es)" if mismatched else ""))
    for key, remote, local in mismatched:
        print(f"  MISMATCH {key}: bucket {remote} bytes, local {local} bytes")


if __name__ == "__main__":
    flags = set(sys.argv[1:])
    root = Path(os.environ.get("CAM_ARCHIVE_ROOT") or (Path.home() / "Library/Mobile Documents/iCloud~md~obsidian/Documents"
                                                       / "Archive - Changes Around Me/Tooling/Web Archive"))
    cal = Path(os.environ.get("CAM_CALENDARS_REPO") or Path(__file__).resolve().parent.parent / "calendars")
    sync(root, cal / "docs" / "captures.json", dry_run="--dry-run" in flags, verify="--verify" in flags,
         purge_old="--purge-old-keys" in flags, prune_deleted="--prune-deleted" in flags)
