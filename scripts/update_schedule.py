#!/usr/bin/env python3
"""Update the SNL schedule in index.html from the TVMaze API.

TVMaze is the single source of truth for host, musical guest, and airdate.
The episode `name` field follows one of three formats:

  "Host / Musical Guest"   -> standard episode
  "Single Name"            -> double-duty (host == guest)
  "TBA"                    -> unannounced; skipped

Existing `note` fields (e.g. "Season finale", "1,000th episode") are
preserved across updates -- TVMaze does not model these, so they remain
human-curated.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from pathlib import Path

TVMAZE_SHOW_ID = 361  # Saturday Night Live
TVMAZE_URL = f"https://api.tvmaze.com/shows/{TVMAZE_SHOW_ID}/episodes"
TVMAZE_SEASONS_URL = f"https://api.tvmaze.com/shows/{TVMAZE_SHOW_ID}/seasons"

# Valid TVMaze date format. Strict validation here means malformed
# or hostile values are dropped at the boundary, before they can flow
# into the rendered JS source.
AIRDATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = REPO_ROOT / "index.html"
CHANGES_MD = REPO_ROOT / "CHANGES.md"

START_MARKER = "// --- SCHEDULE:START (auto-updated by scripts/update_schedule.py) ---"
END_MARKER = "// --- SCHEDULE:END ---"


# ---------- TVMaze fetch & parse ---------------------------------------------

def fetch_episodes() -> list[dict]:
    """Fetch all SNL episodes from TVMaze. Fails loudly on network/HTTP errors."""
    req = urllib.request.Request(
        TVMAZE_URL,
        headers={"User-Agent": "is-snl-new (github.com schedule updater)"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        if resp.status != 200:
            raise RuntimeError(f"TVMaze returned HTTP {resp.status}")
        return json.load(resp)


def fetch_seasons() -> list[dict]:
    """Fetch all SNL season metadata from TVMaze.

    Each season object includes 'number', 'premiereDate', and 'endDate'.
    endDate is null until the season is marked as concluded on TVMaze --
    typically populated within a few days of the actual finale airing.
    """
    req = urllib.request.Request(
        TVMAZE_SEASONS_URL,
        headers={"User-Agent": "is-snl-new (github.com schedule updater)"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        if resp.status != 200:
            raise RuntimeError(f"TVMaze /seasons returned HTTP {resp.status}")
        return json.load(resp)


def season_end_date(seasons: list[dict], number: int) -> str | None:
    """Return the strict-ISO endDate for a given season number, or None.

    Validates the format defensively: TVMaze sometimes returns null,
    and we never want to inject anything other than YYYY-MM-DD downstream.
    """
    for s in seasons:
        if s.get("number") == number:
            ed = s.get("endDate")
            if isinstance(ed, str) and AIRDATE_RE.match(ed):
                return ed
            return None
    return None


def latest_season(episodes: list[dict]) -> int:
    """Highest season number that has at least one airdate."""
    seasons = {ep["season"] for ep in episodes if ep.get("airdate") and ep.get("season")}
    if not seasons:
        raise RuntimeError("No episodes with airdates found in TVMaze response")
    return max(seasons)


def parse_title(title: str | None) -> tuple[str, str, bool] | None:
    """Parse 'Host / Guest' or 'Single Name'. Returns (host, guest, double) or None for TBA/empty."""
    if not title:
        return None
    title = title.strip()
    if not title or title.upper() == "TBA":
        return None
    if " / " in title:
        host, guest = (s.strip() for s in title.split(" / ", 1))
        if not host or not guest:
            return None
        # Defensive: if someone hand-edited TVMaze to write "Name / Name"
        return (host, guest, host.lower() == guest.lower())
    # Single name = double-duty
    return (title, title, True)


# ---------- TVMaze parse -----------------------------------------------------

def episode_to_entry(ep: dict) -> dict | None:
    """Convert a TVMaze episode object to our schedule entry shape."""
    airdate = ep.get("airdate")
    if not airdate or not AIRDATE_RE.match(str(airdate)):
        return None
    season = ep.get("season")
    if not isinstance(season, int):
        return None
    parsed = parse_title(ep.get("name"))
    if parsed is None:
        return None
    host, guest, double = parsed
    entry = {"date": airdate, "season": season, "host": host, "guest": guest}
    if double:
        entry["double"] = True
    return entry


# ---------- existing schedule extraction -------------------------------------

# Tolerant matcher for entries like:
#   { date: "2025-10-04", host: "Bad Bunny", guest: "Doja Cat" },
#   { date: "2026-01-31", host: "Alexander Skarsgård", guest: "Cardi B", note: "1,000th episode" },
ENTRY_RE = re.compile(r"\{[^{}]*\}")


def parse_existing_schedule(html: str) -> list[dict]:
    block_re = re.compile(
        re.escape(START_MARKER) + r"(.*?)" + re.escape(END_MARKER),
        re.DOTALL,
    )
    m = block_re.search(html)
    if not m:
        sys.exit(f"FATAL: schedule markers not found in {INDEX_HTML}")
    body = m.group(1)

    entries: list[dict] = []
    for match in ENTRY_RE.finditer(body):
        # Convert JS-ish object to JSON: quote bare keys, normalize true/false.
        # Keys are simple identifiers (date, host, guest, double, note).
        obj_text = re.sub(r"(\b[a-z]+\b)\s*:", r'"\1":', match.group(0))
        try:
            entries.append(json.loads(obj_text))
        except json.JSONDecodeError as e:
            print(f"WARN: could not parse existing entry: {match.group(0)} ({e})")
    return entries


# ---------- merge ------------------------------------------------------------

def merge(existing: list[dict], fresh: list[dict], current_season: int) -> list[dict]:
    """Merge fresh entries into existing, keyed by date.

    Rules:
      - Drop existing entries from previous seasons (different from
        current_season). When TVMaze rolls over to a new season, the old
        season's episodes are removed in one PR.
      - Fresh values overwrite host / guest / double / season.
      - Existing notes are preserved (TVMaze doesn't track them).
      - Existing-only entries within the current season are kept.
    """
    by_date: dict[str, dict] = {
        e["date"]: dict(e)
        for e in existing
        if e.get("season") == current_season
    }
    for entry in fresh:
        date = entry["date"]
        if date in by_date:
            preserved_note = by_date[date].get("note")
            # Reset double flag if fresh says no longer double-duty
            by_date[date].pop("double", None)
            by_date[date].update(entry)
            if preserved_note:
                by_date[date]["note"] = preserved_note
        else:
            by_date[date] = dict(entry)
    return sorted(by_date.values(), key=lambda e: e["date"])


def diff(old: list[dict], new: list[dict]) -> tuple[list[dict], list[tuple[dict, dict]], list[dict]]:
    """Return (added, changed, removed)."""
    old_by_date = {e["date"]: e for e in old}
    new_by_date = {e["date"]: e for e in new}

    added = [new_by_date[d] for d in new_by_date if d not in old_by_date]
    removed = [old_by_date[d] for d in old_by_date if d not in new_by_date]
    changed: list[tuple[dict, dict]] = []
    for d, n in new_by_date.items():
        if d not in old_by_date:
            continue
        o = old_by_date[d]
        if (
            o.get("host") != n.get("host")
            or o.get("guest") != n.get("guest")
            or bool(o.get("double")) != bool(n.get("double"))
            or o.get("season") != n.get("season")
            or o.get("note") != n.get("note")
        ):
            changed.append((o, n))
    return added, changed, removed


# ---------- render -----------------------------------------------------------

def render_entry(entry: dict) -> str:
    parts = [
        f"date: {json.dumps(entry['date'])}",
        f"season: {json.dumps(entry['season'])}",
        f"host: {json.dumps(entry['host'], ensure_ascii=False)}",
        f"guest: {json.dumps(entry['guest'], ensure_ascii=False)}",
    ]
    if entry.get("double"):
        parts.append("double: true")
    if entry.get("note"):
        parts.append(f"note: {json.dumps(entry['note'], ensure_ascii=False)}")
    return "    { " + ", ".join(parts) + " },"


def render_block(entries: list[dict]) -> str:
    """Render the schedule block including START/END markers.

    The regex substitution captures the existing markers and content
    between them (preserving the indent before START_MARKER), so we
    only need correct indents for entries and the closing marker.
    """
    parts = [START_MARKER]
    parts.extend(render_entry(e) for e in entries)
    parts.append("    " + END_MARKER)
    return "\n".join(parts)


# ---------- main -------------------------------------------------------------

def main() -> int:
    html = INDEX_HTML.read_text(encoding="utf-8")
    existing = parse_existing_schedule(html)

    episodes = fetch_episodes()
    season = latest_season(episodes)
    fresh = [e for e in (episode_to_entry(ep) for ep in episodes if ep.get("season") == season) if e]

    if not fresh:
        print(f"No usable episodes found for season {season} (all TBA?). Nothing to do.")
        return 0

    # Use the /seasons endpoint to identify the season finale. TVMaze
    # populates `endDate` once the season is marked as concluded; until
    # then it's null and we don't tag anything. This avoids the false
    # positives a "last-known-episode" heuristic produces during hiatuses.
    finale_date = None
    try:
        seasons_data = fetch_seasons()
        finale_date = season_end_date(seasons_data, season)
    except Exception as e:
        print(f"WARN: Failed to fetch /seasons ({e}). Skipping finale detection.")

    if finale_date:
        for entry in fresh:
            if entry["date"] == finale_date:
                entry["note"] = "Season finale"
                break
        else:
            print(
                f"WARN: /seasons reports endDate={finale_date} for season {season}, "
                f"but no episode in /episodes matches that date."
            )

    merged = merge(existing, fresh, season)
    added, changed, removed = diff(existing, merged)

    if not added and not changed and not removed:
        print(f"Season {season}: no schedule changes.")
        # Clear any stale CHANGES.md from a prior run
        CHANGES_MD.unlink(missing_ok=True)
        return 0

    # Render and replace
    new_block = render_block(merged)
    block_re = re.compile(
        re.escape(START_MARKER) + r".*?" + re.escape(END_MARKER),
        re.DOTALL,
    )
    new_html = block_re.sub(new_block, html, count=1)
    INDEX_HTML.write_text(new_html, encoding="utf-8")

    # PR body
    lines = [
        f"Updated SNL Season {season} schedule from TVMaze.",
        "",
        f"Source: <{TVMAZE_URL}>",
        "",
    ]
    if removed:
        # Group by removed-entry season for clarity when seasons roll over
        removed_seasons = sorted(
            {s for e in removed if (s := e.get("season")) is not None}
        )
        if removed_seasons and all(s != season for s in removed_seasons):
            lines.append(
                f"### Removed (rolling over to season {season})"
            )
        else:
            lines.append("### Removed")
        for e in removed:
            s = e.get("season", "?")
            lines.append(f"- **{e['date']}** (S{s}): {e.get('host', '?')} / {e.get('guest', '?')}")
        lines.append("")
    if added:
        lines.append("### Added")
        for e in added:
            tag = " (double duty)" if e.get("double") else ""
            note = f" — *{e['note']}*" if e.get("note") else ""
            lines.append(f"- **{e['date']}**: {e['host']} / {e['guest']}{tag}{note}")
        lines.append("")
    if changed:
        lines.append("### Changed")
        for old, new in changed:
            old_label = f"{old.get('host', '?')} / {old.get('guest', '?')}"
            new_label = f"{new['host']} / {new['guest']}"
            note_change = ""
            if old.get("note") != new.get("note"):
                old_note = old.get("note") or "(none)"
                new_note = new.get("note") or "(none)"
                note_change = f" — note: *{old_note}* → *{new_note}*"
            lines.append(f"- **{new['date']}**: {old_label} → {new_label}{note_change}")
        lines.append("")
    lines.append("Review carefully — TVMaze can be edited by anyone.")

    body = "\n".join(lines)
    CHANGES_MD.write_text(body, encoding="utf-8")
    print(body)

    # Set GitHub Actions output if running in CI
    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        with open(gh_output, "a", encoding="utf-8") as f:
            f.write("has_changes=true\n")
            f.write(f"season={season}\n")
            f.write(f"added_count={len(added)}\n")
            f.write(f"changed_count={len(changed)}\n")
            f.write(f"removed_count={len(removed)}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
