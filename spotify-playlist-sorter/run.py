#!/usr/bin/env python3
"""
Spotify Playlist Sorter — scheduled task (run.py)

Daily job that:
1. Fetches the user's liked songs
2. Looks at the top N owned playlists (excl. the first "catch-all/liked" one)
3. For each unprocessed liked song, classifies it by genre and adds to a playlist
4. Tracks processed track IDs so songs are never re-processed

Classification modes (set via manifest config.classification_mode):
  standard     — uses Spotify artist genre tags
  experimental — uses Claude AI to classify based on song metadata
  both         — runs standard first, falls back to Claude for unmatched songs

Exit 0 = success. Exit 1 = error.
"""

import json
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

try:
    from auth import API_BASE, session
except ImportError:
    print("ERROR: Cannot import from spotify-control. Check skills_ref in manifest.json.")
    sys.exit(1)

# ── Paths & config ─────────────────────────────────────────────────────────────

STORAGE_DIR = Path(os.environ.get(
    "TASK_STORAGE_DIR",
    Path(__file__).parent.parent.parent / "storage" / "spotify-playlist-sorter"
))
STATE_FILE = STORAGE_DIR / "state.json"
REPORT_FILE = STORAGE_DIR / "last_run_report.json"

MANIFEST_FILE = Path(__file__).parent / "manifest.json"
manifest = json.loads(MANIFEST_FILE.read_text())
config = manifest.get("config", {})

MAX_PLAYLISTS = config.get("max_playlists_to_consider", 3)
MODE = config.get("classification_mode", "standard")
EXPERIMENTAL = config.get("experimental_claude_classification", True)
DRY_RUN = config.get("dry_run", False)

if DRY_RUN:
    print("⚠️  DRY RUN — no changes will be made to Spotify")

# ── State ──────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"processed_track_ids": [], "last_run": None}


def save_state(state: dict):
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


# ── Spotify helpers ────────────────────────────────────────────────────────────

def get_liked_songs() -> list[dict]:
    """Return all liked songs as list of track dicts."""
    s = session()
    tracks = []
    url = f"{API_BASE}/me/tracks?limit=50"
    while url:
        r = s.get(url)
        r.raise_for_status()
        data = r.json()
        for item in data["items"]:
            t = item.get("track")
            if t and t.get("id"):
                tracks.append(t)
        url = data.get("next")
        if url:
            time.sleep(0.1)
    return tracks


def get_my_playlists(limit: int = 50) -> list[dict]:
    """Return playlists owned by the current user."""
    s = session()
    me_r = s.get(f"{API_BASE}/me")
    me_r.raise_for_status()
    my_id = me_r.json()["id"]

    playlists = []
    url = f"{API_BASE}/me/playlists?limit=50"
    while url:
        r = s.get(url)
        r.raise_for_status()
        data = r.json()
        for p in data["items"]:
            if p and p.get("owner", {}).get("id") == my_id:
                playlists.append(p)
        url = data.get("next")
        if len(playlists) >= limit:
            break
    return playlists


def get_all_playlist_track_ids(playlist_id: str) -> set[str]:
    """Return set of all track IDs already in a playlist."""
    s = session()
    ids = set()
    url = f"{API_BASE}/playlists/{playlist_id}/items?limit=100&fields=next,items(track(id))"
    while url:
        r = s.get(url)
        r.raise_for_status()
        data = r.json()
        for item in data.get("items", []):
            t = item.get("track")
            if t and t.get("id"):
                ids.add(t["id"])
        url = data.get("next")
        if url:
            time.sleep(0.1)
    return ids


def get_artist_genres(artist_id: str) -> list[str]:
    s = session()
    r = s.get(f"{API_BASE}/artists/{artist_id}")
    if not r.ok:
        return []
    return r.json().get("genres", [])


def add_tracks_to_playlist(playlist_id: str, track_uris: list[str]):
    if DRY_RUN:
        return
    s = session()
    for i in range(0, len(track_uris), 100):
        batch = track_uris[i:i+100]
        r = s.post(f"{API_BASE}/playlists/{playlist_id}/items", json={"uris": batch})
        r.raise_for_status()
        time.sleep(0.1)


# ── Genre matching (standard mode) ────────────────────────────────────────────

def score_track_for_playlist(genres: list[str], playlist_name: str, playlist_desc: str = "") -> int:
    """
    Simple keyword scoring: how well do the track's genres match the playlist.
    Higher = better match.
    """
    target = (playlist_name + " " + playlist_desc).lower()
    score = 0
    for genre in genres:
        words = re.findall(r'\w+', genre.lower())
        for word in words:
            if len(word) > 3 and word in target:
                score += 2
        # Partial substring match
        if any(word in target for word in words if len(word) > 3):
            score += 1
    return score


def classify_standard(track: dict, target_playlists: list[dict]) -> str | None:
    """Return the best matching playlist_id, or None if no good match."""
    artist_ids = [a["id"] for a in track.get("artists", []) if a.get("id")]
    all_genres = []
    for aid in artist_ids[:2]:  # limit API calls
        all_genres.extend(get_artist_genres(aid))
        time.sleep(0.05)

    if not all_genres:
        return None

    best_id = None
    best_score = 0
    for p in target_playlists:
        score = score_track_for_playlist(all_genres, p["name"], p.get("description", ""))
        if score > best_score:
            best_score = score
            best_id = p["id"]

    return best_id if best_score >= 2 else None


# ── Claude classification (experimental mode) ──────────────────────────────────

def classify_with_claude(tracks: list[dict], target_playlists: list[dict]) -> dict[str, str]:
    """
    Ask Claude to classify a batch of tracks into playlists.
    Returns {track_id: playlist_id} mapping.
    """
    playlist_info = [
        {"id": p["id"], "name": p["name"], "description": p.get("description", "")}
        for p in target_playlists
    ]

    track_info = []
    for t in tracks:
        artists = ", ".join(a["name"] for a in t.get("artists", []))
        track_info.append({
            "id": t["id"],
            "name": t["name"],
            "artists": artists,
            "album": t.get("album", {}).get("name", ""),
        })

    prompt = f"""You are classifying songs into music playlists by genre.

Available playlists:
{json.dumps(playlist_info, indent=2)}

Songs to classify:
{json.dumps(track_info, indent=2)}

For each song, decide which playlist it best fits based on the song name, artist(s), and album.
If a song doesn't fit any playlist well, use "skip".

Return ONLY a JSON object mapping track IDs to playlist IDs (or "skip"). Example:
{{"track_id_1": "playlist_id_2", "track_id_2": "skip"}}

No explanation, no markdown — just the raw JSON object."""

    try:
        result = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            print(f"Claude classification failed: {result.stderr[:300]}")
            return {}

        # Extract JSON from Claude's response
        raw = result.stdout.strip()
        # Find the JSON object in the response
        match = re.search(r'\{[^{}]*\}', raw, re.DOTALL)
        if not match:
            print(f"Could not parse Claude response: {raw[:300]}")
            return {}

        mapping = json.loads(match.group())
        # Validate: only keep entries with real playlist IDs
        valid_ids = {p["id"] for p in target_playlists}
        return {
            tid: pid
            for tid, pid in mapping.items()
            if pid in valid_ids
        }

    except subprocess.TimeoutExpired:
        print("Claude classification timed out")
        return {}
    except json.JSONDecodeError as e:
        print(f"JSON parse error in Claude response: {e}")
        return {}
    except Exception as e:
        print(f"Claude classification error: {e}")
        return {}


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    state = load_state()
    processed_ids = set(state.get("processed_track_ids", []))

    print(f"Mode: {MODE} | Experimental Claude: {EXPERIMENTAL}")
    print(f"Previously processed: {len(processed_ids)} tracks")

    # 1. Get target playlists (top N owned playlists)
    print("\nFetching playlists...")
    all_playlists = get_my_playlists()
    if not all_playlists:
        print("No owned playlists found.")
        sys.exit(0)

    # Take top MAX_PLAYLISTS, but build a catalog of ALL playlist track IDs
    # to know if a song is already in any playlist
    target_playlists = all_playlists[:MAX_PLAYLISTS]
    print(f"Target playlists ({len(target_playlists)}):")
    for p in target_playlists:
        print(f"  • {p['name']} ({p['id']})")

    print("\nBuilding existing track catalog...")
    tracks_in_playlists: set[str] = set()
    for p in target_playlists:
        ids = get_all_playlist_track_ids(p["id"])
        tracks_in_playlists.update(ids)
        print(f"  {p['name']}: {len(ids)} tracks")

    # 2. Get liked songs
    print("\nFetching liked songs...")
    liked = get_liked_songs()
    print(f"Total liked songs: {len(liked)}")

    # 3. Filter to unprocessed songs not already in a target playlist
    to_process = [
        t for t in liked
        if t["id"] not in processed_ids
        and t["id"] not in tracks_in_playlists
    ]
    print(f"Songs to classify: {len(to_process)}")

    if not to_process:
        print("Nothing to do — all liked songs are already classified.")
        state["last_run"] = datetime.now().isoformat()
        save_state(state)
        sys.exit(0)

    # 4. Classify
    assignments: dict[str, str] = {}  # track_id -> playlist_id

    if MODE in ("standard", "both"):
        print("\nRunning standard (Spotify genre) classification...")
        for i, track in enumerate(to_process):
            pid = classify_standard(track, target_playlists)
            if pid:
                assignments[track["id"]] = pid
                artists = ", ".join(a["name"] for a in track.get("artists", []))
                pl_name = next((p["name"] for p in target_playlists if p["id"] == pid), pid)
                print(f"  [{i+1}/{len(to_process)}] {track['name']} — {artists} → {pl_name}")
            time.sleep(0.05)

    unmatched = [t for t in to_process if t["id"] not in assignments]

    if EXPERIMENTAL and (MODE == "experimental" or (MODE == "both" and unmatched)):
        classify_targets = to_process if MODE == "experimental" else unmatched
        print(f"\nRunning experimental (Claude) classification on {len(classify_targets)} tracks...")
        # Process in batches of 20
        for i in range(0, len(classify_targets), 20):
            batch = classify_targets[i:i+20]
            claude_assignments = classify_with_claude(batch, target_playlists)
            assignments.update(claude_assignments)
            print(f"  Batch {i//20 + 1}: classified {len(claude_assignments)} tracks")
            if i + 20 < len(classify_targets):
                time.sleep(1)

    # 5. Add to playlists
    by_playlist: dict[str, list] = defaultdict(list)
    for track_id, playlist_id in assignments.items():
        by_playlist[playlist_id].append(f"spotify:track:{track_id}")

    print("\nAdding tracks to playlists...")
    added_count = 0
    for playlist_id, uris in by_playlist.items():
        pl_name = next((p["name"] for p in target_playlists if p["id"] == playlist_id), playlist_id)
        print(f"  → {pl_name}: {len(uris)} tracks {'(dry run)' if DRY_RUN else ''}")
        add_tracks_to_playlist(playlist_id, uris)
        added_count += len(uris)

    # 6. Update state — mark ALL to_process as seen (even unmatched, to avoid re-checking)
    new_processed = processed_ids | {t["id"] for t in to_process}
    state["processed_track_ids"] = list(new_processed)
    state["last_run"] = datetime.now().isoformat()
    save_state(state)

    # 7. Write report
    report = {
        "run_at": datetime.now().isoformat(),
        "mode": MODE,
        "experimental_used": EXPERIMENTAL,
        "liked_songs_total": len(liked),
        "songs_processed": len(to_process),
        "songs_assigned": added_count,
        "songs_skipped": len(to_process) - len(assignments),
        "by_playlist": {pid: len(uris) for pid, uris in by_playlist.items()},
        "dry_run": DRY_RUN,
    }
    REPORT_FILE.write_text(json.dumps(report, indent=2))

    print(f"\n✓ Done: {added_count} tracks added across {len(by_playlist)} playlists.")
    print(f"  Skipped (no match): {len(to_process) - len(assignments)}")


if __name__ == "__main__":
    main()
