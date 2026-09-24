#!/usr/bin/env python3
"""Fill the link tables on the vault's Organizations pages from the registry.

    python orgpages.py [--dry-run] [--vault=DIR] [--calendars=DIR] [--offline]

Ian writes the organisation pages (Organizations/<name>.md) by hand: which
pages are listed, under which heading, in what order, with what name. This
fills in the cells that are facts about monitoring, and nothing else:

    | Page | Checked | Last changed |            <- the columns this looks for
    | [Service changes](https://www.capmetro.org/servicechange)<br>[2026-09-21](https://archive.../2026-09-21-2255.pdf) | 2026-09-22 06:31 | 2026-09-21 22:55 |

A row is matched to the registry by the URL of the first markdown link in
its first cell (caltools.registry.match_slug — the build's own matcher, so
trailing slashes, fragments and query noise do not matter). Matched rows get:

  Checked        newest of: the build's last fetch (docs/status.json) and the
                 archive job's last visit (docs/captures.json), across the page
                 and its child pages
  Last changed   newest of: the build's last content change and the newest
                 capture of a page captured more than once (a capture is only
                 taken when the text changed) — or "None since <date>" when no
                 change has been seen since watching began (status.json and
                 captures.json `since`, or the first capture)
  Archive        when the table has this column: a link to the newest capture
                 (latest.pdf). When it does not, the capture links go under the
                 page link in the first cell instead — one line per day, newest
                 first, at most CAPTURE_LINES — but only once a page has two or
                 more captures, i.e. has changed while we watched. A page
                 captured once reads the same as the live page; its stable
                 latest.pdf covers "a copy in case the page disappears".

A row whose URL is not in the registry is left exactly as it is (a dead link,
a plain reference) and reported. A table without a Checked column is not a
link table and is skipped (board members, say). Everything outside the
matched cells — the page name, footnotes, notes after a <br>, other columns,
callout prefixes — is preserved byte for byte; a page is rewritten only when
a cell actually changes.

status.json is fetched from the calendars repo on GitHub (the build commits
it there twice a day) so Checked is current even when the local checkout is
behind; the local copy is the fallback (and --offline forces it).

Runs at the end of every batch (archive_page.py --all / --due) when the vault
is mounted, and on its own with --pages.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

CALENDARS_REPO = Path(os.environ.get("CAM_CALENDARS_REPO") or Path(__file__).resolve().parent.parent / "calendars")
VAULT_ROOT = Path(os.environ.get("CAM_VAULT_ROOT") or Path.home() / "Obsidian Sync" / "Changes Around Me")
STATUS_URL = "https://raw.githubusercontent.com/changesaroundme/calendars/main/docs/status.json"
CENTRAL = ZoneInfo("America/Chicago")
CAPTURE_LINES = 3                 # capture links under a page name (newest first)
STALE_DAYS = 2                    # warn when status.json is older than this

LINK_RE = re.compile(r"\[([^\]]*)\]\((https?://[^)\s]+)\)")
CAPTURE_LINE_RE = re.compile(r"(?:<br>)?\[\d{4}-\d{2}-\d{2}\]\(https?://[^)\s]+\.pdf\)")   # lines this script writes
TABLE_ROW_RE = re.compile(r"^(\s*(?:>\s*)*)\|(.*)\|\s*$")                                 # optional callout prefix
SEP_CELL_RE = re.compile(r"^\s*:?-{3,}:?\s*$")
CELL_SPLIT_RE = re.compile(r"(?<!\\)\|")
COLUMN_NAMES = {"checked": "checked", "last changed": "changed", "archive": "archive"}


def _registry():
    if str(CALENDARS_REPO) not in sys.path:
        sys.path.insert(0, str(CALENDARS_REPO))
    from caltools import registry
    return registry


def load_status(calendars: Path, offline: bool = False) -> tuple[dict, str]:
    """docs/status.json from GitHub (current) else the local checkout; returns (doc, where)."""
    local = calendars / "docs" / "status.json"
    if not offline:
        try:
            with urlopen(Request(STATUS_URL, headers={"User-Agent": "cam-webarchive orgpages"}), timeout=20) as r:
                return json.load(r), "GitHub"
        except Exception as e:
            print(f"  (status.json from GitHub unavailable: {e}; using the local checkout)")
    try:
        return json.loads(local.read_text()), "local checkout"
    except (OSError, ValueError):
        return {"generated": "", "sources": {}}, "none"


def _json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _utc(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _local(stamp: str) -> datetime | None:
    """archive.json stamps are local capture time: 2026-09-21-2255."""
    try:
        return datetime.strptime(stamp, "%Y-%m-%d-%H%M").replace(tzinfo=CENTRAL)
    except ValueError:
        return None


def fmt(dt: datetime | None) -> str:
    return dt.astimezone(CENTRAL).strftime("%Y-%m-%d %H:%M") if dt else "—"


class Facts:
    """What is known about each registry page, from the three generated files."""

    def __init__(self, rows: list[dict], status: dict, captures: dict, archive: dict):
        self.rows = rows
        self.children: dict[str, list[str]] = {}
        for r in rows:
            if r["parent"]:
                self.children.setdefault(r["parent"], []).append(r["slug"])
        self.status = status.get("sources", {})
        self.captures = captures.get("sources", {})
        self.archive = archive.get("sources", {})

    def group(self, slug: str) -> list[str]:
        return [slug] + self.children.get(slug, [])

    def checked(self, slug: str) -> datetime | None:
        stamps = [_utc(self.status.get(s, {}).get("checked")) for s in self.group(slug)]
        stamps += [_utc(self.captures.get(s, {}).get("checked")) for s in self.group(slug)]
        return max((s for s in stamps if s), default=None)

    def changed(self, slug: str) -> datetime | None:
        """The newest change seen, across the page and its child pages: the
        build's last content change, or the newest capture of a page captured
        more than once (a capture is only taken when the text changed). None
        when no change has been seen."""
        stamps = [_utc(self.status.get(s, {}).get("changed")) for s in self.group(slug)]
        for s in self.group(slug):
            caps = self.archive.get(s, {}).get("captures", [])
            if len(caps) > 1:
                stamps.append(_local(max(c["stamp"] for c in caps)))
        return max((s for s in stamps if s), default=None)

    def watched_since(self, slug: str) -> datetime | None:
        """When watching began, for "None since": the earliest of the build's
        baseline for the current hashing (status.json `since`), the archive
        job's (captures.json `since`, kept when duplicates are removed) and
        the first capture still in the archive."""
        stamps = [_utc(self.status.get(s, {}).get("since")) for s in self.group(slug)]
        stamps += [_utc(self.captures.get(s, {}).get("since")) for s in self.group(slug)]
        for s in self.group(slug):
            caps = self.archive.get(s, {}).get("captures", [])
            if caps:
                stamps.append(_local(min(c["stamp"] for c in caps)))
            elif s in self.captures:                      # captured, but not mirrored yet
                stamps.append(_utc(self.captures[s].get("captured")))
        return min((s for s in stamps if s), default=None)

    def changed_cell(self, slug: str) -> str:
        """Last changed: a date, or "None since <date>" when no change has
        been seen since watching began, or "" when nothing is known yet."""
        if (dt := self.changed(slug)):
            return fmt(dt)
        since = self.watched_since(slug)
        return f"None since {since.astimezone(CENTRAL):%Y-%m-%d}" if since else ""

    def capture_lines(self, slug: str, limit: int = CAPTURE_LINES) -> list[str]:
        """[YYYY-MM-DD](url) per capture, newest first, one per day — none
        for a page captured only once (it has not changed while watched).
        Duplicate captures (a late-loading widget, screen-reader-only text)
        are weeded out of the archive by hand, not filtered here."""
        caps = sorted(self.archive.get(slug, {}).get("captures", []), key=lambda c: c["stamp"], reverse=True)
        if len(caps) < 2:
            return []
        out, seen = [], set()
        for c in caps:
            day = c["stamp"][:10]
            if day in seen:
                continue
            seen.add(day)
            out.append(f"[{day}]({c['url']})")
            if len(out) == limit:
                break
        return out

    def latest(self, slug: str) -> str:
        return self.archive.get(slug, {}).get("latest") or self.captures.get(slug, {}).get("url", "")


def split_cells(body: str) -> list[str]:
    return CELL_SPLIT_RE.split(body)


def refresh_page(text: str, facts: Facts, registry) -> tuple[str, list[str], list[str]]:
    """Returns (new text, matched slugs, unmatched urls and skipped-table notes)."""
    lines = text.split("\n")
    out, matched, unmatched, problems = [], [], [], []
    i = 0
    while i < len(lines):
        m = TABLE_ROW_RE.match(lines[i])
        nxt = TABLE_ROW_RE.match(lines[i + 1]) if i + 1 < len(lines) else None
        is_header = (m and nxt and all(SEP_CELL_RE.match(c) for c in split_cells(nxt.group(2))))
        if not is_header:
            out.append(lines[i]); i += 1
            continue
        header = [c.strip().lower() for c in split_cells(m.group(2))]
        cols = {COLUMN_NAMES[h]: k for k, h in enumerate(header) if h in COLUMN_NAMES}
        if "checked" not in cols:                       # not a link table
            out.append(lines[i]); i += 1
            continue
        link_col = next((k for k, h in enumerate(header) if h == "page"), None)
        if link_col is None or link_col in cols.values():
            # A table with Checked but no Page column is malformed (LCRA, Sep
            # 2026: the header was missing its first cell). Touching it would
            # overwrite links with dates — leave it and say so.
            problems.append(f"table at line {i + 1} has no Page column (header: {' | '.join(header)})")
            out.append(lines[i]); i += 1
            continue
        out += [lines[i], lines[i + 1]]
        i += 2
        while i < len(lines) and (row := TABLE_ROW_RE.match(lines[i])):
            prefix, body = row.group(1), row.group(2)
            cells = split_cells(body)
            link = LINK_RE.search(cells[link_col]) if len(cells) > link_col else None
            slug = registry.match_slug(link.group(2), facts.rows) if link else None
            if not slug:
                if link:
                    unmatched.append(link.group(2))
                out.append(lines[i]); i += 1
                continue
            matched.append(slug)
            width = [len(c) for c in cells]                 # keep the hand-aligned column widths where possible
            first = CAPTURE_LINE_RE.sub("", cells[link_col]).rstrip()
            if "archive" not in cols:
                caps = facts.capture_lines(slug)
                if caps:
                    first = first + "<br>" + "<br>".join(caps)
            cells[link_col] = " " + first.strip() + " "
            for key, k in cols.items():
                if k >= len(cells):
                    continue
                if key == "checked":
                    val = fmt(facts.checked(slug))
                elif key == "changed":
                    val = facts.changed_cell(slug)
                else:
                    url = facts.latest(slug)
                    day = (facts.archive.get(slug, {}).get("captures") or [{"stamp": ""}])[0]["stamp"][:10]
                    val = f"[{day}]({url})" if url and day else ""
                if not val or val == "—":
                    continue                            # nothing known yet: leave Ian's placeholder as it is
                cells[k] = " " + val.ljust(max(width[k] - 2, len(val))) + " "
            out.append(f"{prefix}|{'|'.join(cells)}|")
            i += 1
    return "\n".join(out), matched, unmatched + [f"(skipped) {p}" for p in problems]


def refresh(vault: Path = VAULT_ROOT, calendars: Path = CALENDARS_REPO, dry_run: bool = False,
            offline: bool = False) -> int:
    registry = _registry()
    rows = registry.load(calendars / "sources.yaml")
    status, where = load_status(calendars, offline)
    facts = Facts(rows, status, _json(calendars / "docs" / "captures.json"), _json(calendars / "docs" / "archive.json"))
    gen = _utc(status.get("generated"))
    age = (datetime.now(timezone.utc) - gen).days if gen else None
    print(f"Organisation pages: status.json from {where}"
          + (f", generated {fmt(gen)}" if gen else "")
          + (f" — {age} days old, Checked/Last changed for build pages may lag" if age is not None and age >= STALE_DAYS else ""))
    folder = vault / "Organizations"
    if not folder.is_dir():
        print(f"  no Organizations folder at {folder}; nothing to do")
        return 0
    seen: set[str] = set()
    changed_pages = 0
    for page in sorted(folder.glob("*.md")):
        text = page.read_text(encoding="utf-8")
        new, matched, unmatched = refresh_page(text, facts, registry)
        seen.update(matched)
        note = f"{len(matched)} matched" + (f", {len(unmatched)} not in the registry" if unmatched else "")
        if new != text:
            changed_pages += 1
            if not dry_run:
                page.write_text(new, encoding="utf-8")
            print(f"  {'would update' if dry_run else 'updated'} {page.name}: {note}")
        elif matched or unmatched:
            print(f"  unchanged {page.name}: {note}")
        for u in unmatched:
            print(f"      {u}" if u.startswith("(skipped)") else f"      not in registry: {u}")
    missing = [r for r in rows if r["public"] == "yes" and r["status"] == "active" and not r["parent"] and r["slug"] not in seen]
    if missing:
        print(f"  registry pages on no organisation page ({len(missing)}):")
        for r in missing:
            print(f"      {r['org']:10} {r['name']}  {r['url']}")
    return changed_pages


if __name__ == "__main__":
    flags = set(a for a in sys.argv[1:] if a.startswith("--"))
    vault = next((Path(a.split("=", 1)[1]).expanduser() for a in flags if a.startswith("--vault=")), VAULT_ROOT)
    cal = next((Path(a.split("=", 1)[1]).expanduser() for a in flags if a.startswith("--calendars=")), CALENDARS_REPO)
    refresh(vault, cal, dry_run="--dry-run" in flags, offline="--offline" in flags)
