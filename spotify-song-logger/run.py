#!/usr/bin/env python3
"""
Spotify Song Logger — daemon task (run.py)

Called every interval_seconds by the host.
Checks the currently playing Spotify track.
If it's a new song (different from last logged), appends to songs.md.
Exit 0 = success (including "nothing playing" or "same song").
Exit 1 = error.
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

# spotify-control is injected into PYTHONPATH by the host runner
try:
    from auth import API_BASE, session
except ImportError:
    print("ERROR: Cannot import from spotify-control. Check skills_ref in manifest.json.")
    sys.exit(1)

# ── Paths ──────────────────────────────────────────────────────────────────────

STORAGE_DIR = Path(os.environ.get("TASK_STORAGE_DIR", Path(__file__).parent.parent.parent / "storage" / "spotify-song-logger"))
SONGS_FILE = STORAGE_DIR / "songs.md"
STATE_FILE = STORAGE_DIR / "state.json"

# ── Helpers ────────────────────────────────────────────────────────────────────

def get_current_track() -> dict | None:
    """Return track info dict if something is actively playing, else None."""
    s = session()
    r = s.get(f"{API_BASE}/me/player/currently-playing")

    if r.status_code == 204 or not r.content:
        return None  # Nothing playing / no active device

    if not r.ok:
        print(f"Spotify API error: {r.status_code} — {r.text[:200]}")
        sys.exit(1)

    data = r.json()

    if not data.get("is_playing"):
        return None

    item = data.get("item")
    if not item:
        return None

    # Handle podcasts / episodes gracefully
    item_type = item.get("type", "track")
    if item_type == "episode":
        return {
            "id": item["id"],
            "name": item["name"],
            "artists": [item.get("show", {}).get("name", "Unknown Podcast")],
            "url": item.get("external_urls", {}).get("spotify", ""),
            "type": "episode",
        }

    return {
        "id": item["id"],
        "name": item["name"],
        "artists": [a["name"] for a in item.get("artists", [])],
        "url": item.get("external_urls", {}).get("spotify", ""),
        "type": "track",
    }


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"last_track_id": None}


def save_state(state: dict):
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def append_song(track: dict):
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    artists = ", ".join(track["artists"])
    emoji = "🎙️" if track["type"] == "episode" else "🎵"
    line = f"- {emoji} [{track['name']}]({track['url']}) — {artists} · {now}\n"

    # Initialize file with header if it doesn't exist
    if not SONGS_FILE.exists():
        SONGS_FILE.write_text("# Spotify Listening Log\n\n")

    with open(SONGS_FILE, "a") as f:
        f.write(line)

    print(f"Logged: {track['name']} — {artists}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    state = load_state()
    track = get_current_track()

    if track is None:
        print("Nothing playing.")
        return

    if track["id"] == state.get("last_track_id"):
        print(f"Same track: {track['name']} — no change.")
        return

    append_song(track)
    state["last_track_id"] = track["id"]
    save_state(state)


if __name__ == "__main__":
    main()
