#!/usr/bin/env python3
"""Mirror the web archive into Cloudflare R2 (or any S3-compatible bucket).

    python r2sync.py [--dry-run] [--verify]

Every capture PDF and every downloaded document goes up once, under a key
that never changes and is never overwritten — the bucket is an append-only
history, so nothing here deletes. Keys are chosen so a bucket listing reads
like the archive itself, newest last:

    <slug>/<YYYY-MM-DD HHMM> - <page title>.pdf              a page capture
    <slug>/attachments/<YYYY-MM-DD HHMM> - <file name>       a document, as fetched then
    _unlisted/<folder name>/...                              a folder with no registry row

The stamp comes first so versions sort by time; the page title / file name
stays so a key is recognisable on its own. For a document the stamp is when
we fetched that version (Attachments/.index.json remembers it; a file the
index does not know is stamped with its modification time). The local files
keep their own names — this mapping lives only here.

Configuration is environment only (never in the repo; run.sh passes it from
~/.config/cam-webarchive/r2.env):
    R2_ENDPOINT     https://<account id>.r2.cloudflarestorage.com
    R2_BUCKET       bucket name
    R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY   an API token scoped to that bucket
    R2_PUBLIC_URL   how the bucket is reachable in a browser, e.g.
                    https://archive.changesaroundme.com (custom domain) or the
                    bucket's r2.dev URL — used for the links in captures.json

After a sync, captures.json gains for each page the public URL of its newest
capture (`url`) so the generated sources table can link the Archive column.
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

STAMP_RE = re.compile(r"^(.*?) - (\d{4}-\d{2}-\d{2} \d{4})\.pdf$")
DOC_STAMP_RE = re.compile(r"^(.*) - (\d{4}-\d{2}-\d{2} \d{4})(\.[^.]+)?$")
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
    return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H%M")


def plan(root: Path, captures: dict) -> list[tuple[Path, str]]:
    """(local file, bucket key) for every capture and document under root."""
    folder_slug = {v.get("folder"): slug for slug, v in captures.get("sources", {}).items() if v.get("folder")}
    out: list[tuple[Path, str]] = []
    for folder in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith((".", "_"))):
        prefix = folder_slug.get(folder.name) or f"_unlisted/{folder.name}"
        for pdf in sorted(folder.glob("*.pdf")):
            m = STAMP_RE.match(pdf.name)
            if not m:
                continue                                    # not one of ours
            out.append((pdf, f"{prefix}/{m.group(2)} - {m.group(1)}.pdf"))
        att = folder / "Attachments"
        if not att.is_dir():
            continue
        index: dict = {}
        try:
            index = json.loads((att / ".index.json").read_text())
        except (OSError, ValueError):
            pass
        fetched = {v["name"]: v.get("fetched") for v in index.values() if v.get("name")}
        for doc in sorted(p for p in att.iterdir() if p.is_file() and not p.name.startswith(".")):
            m = DOC_STAMP_RE.match(doc.name)
            if m:                                           # a later version already carries its stamp
                stamp, name = m.group(2), m.group(1) + (m.group(3) or "")
            else:
                stamp, name = fetched.get(doc.name) or _fs_stamp(doc), doc.name
            out.append((doc, f"{prefix}/attachments/{stamp} - {name}"))
    return out


def public_url(key: str) -> str:
    base = os.environ.get("R2_PUBLIC_URL", "").rstrip("/")
    return f"{base}/{quote(key)}" if base else ""


def sync(root: Path, captures_path: Path, dry_run: bool = False, verify: bool = False) -> None:
    captures = json.loads(captures_path.read_text()) if captures_path.exists() else {"sources": {}}
    bucket = os.environ["R2_BUCKET"]
    s3 = _client()
    have = existing_keys(s3, bucket)
    todo = plan(root, captures)
    uploaded, skipped, mismatched = 0, 0, []
    for path, key in todo:
        size = path.stat().st_size
        if key in have:
            if verify and have[key] != size:
                mismatched.append((key, have[key], size))
            skipped += 1
            continue
        ctype = CONTENT_TYPES.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if dry_run:
            print(f"  would upload {key} ({size/1e6:.1f} MB)")
        else:
            s3.upload_file(str(path), bucket, key, ExtraArgs={"ContentType": ctype})
            print(f"  uploaded {key} ({size/1e6:.1f} MB)")
        uploaded += 1
    # captures.json: link each page's newest capture
    if not dry_run:
        by_slug: dict[str, str] = {}
        for path, key in todo:
            slug, _, rest = key.partition("/")
            if "/" not in rest and rest.endswith(".pdf") and (slug not in by_slug or rest > by_slug[slug].partition("/")[2]):
                by_slug[slug] = key
        for slug, entry in captures.get("sources", {}).items():
            if slug in by_slug and os.environ.get("R2_PUBLIC_URL"):
                entry["url"] = public_url(by_slug[slug])
        captures_path.write_text(json.dumps(captures, indent=1) + "\n")
    print(f"R2 sync: {uploaded} {'to upload' if dry_run else 'uploaded'}, {skipped} already there"
          + (f", {len(mismatched)} size mismatch(es)" if mismatched else ""))
    for key, remote, local in mismatched:
        print(f"  MISMATCH {key}: bucket {remote} bytes, local {local} bytes")


if __name__ == "__main__":
    flags = set(sys.argv[1:])
    root = Path(os.environ.get("CAM_ARCHIVE_ROOT") or (Path.home() / "Library/Mobile Documents/iCloud~md~obsidian/Documents"
                                                       / "Archive - Changes Around Me/Tooling/Web Archive"))
    cal = Path(os.environ.get("CAM_CALENDARS_REPO") or Path(__file__).resolve().parent.parent / "calendars")
    sync(root, cal / "docs" / "captures.json", dry_run="--dry-run" in flags, verify="--verify" in flags)
