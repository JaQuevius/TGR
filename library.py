"""Soundbite track and playlist library.

Loads playlists and audio paths from folders under ``static/audio`` and
provides title normalization for answer matching.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

@dataclass
class Track:
    id: int
    title: str
    playlist_id: str


@dataclass
class Playlist:
    id: str
    name: str
    tracks: List[Track]


def load_playlists():
    p = Path('static/audio')
    playlists = []
    audio_store = {}
    current_id = 0
    current_playlist_id = 0
    for x in p.iterdir():
        if x.is_dir():
            current_playlist_id += 1
            playlist=Playlist(id=f"{x.stem}-{current_playlist_id}", name=x.stem, tracks=[])
            # Process each subdirectory as a playlist
            for i in x.iterdir():
                if i.is_file() and i.suffix in ['.mp3', '.wav']:
                    # Process each audio file as a track
                    current_id += 1
                    playlist.tracks.append(Track(id=current_id, title=i.stem, playlist_id=playlist.id))
                    audio_store[str(current_id)] = i
            playlists.append(playlist)
    return playlists, audio_store

# File paths indexed by string track ID for /audio/<id> requests.
PLAYLISTS, AUDIO_STORE = load_playlists()

def normalize(value: str) -> str:
    """Normalize a song title or guess for fuzzy-tolerant matching.

    - Strips parenthetical text like `(...)`
    - Strips `feat.` / `ft.` and following text
    - Strips non-alphanumeric characters and lowercases
    """
    if not value:
        return ""
    text = value.lower()
    # Remove parenthetical comments like (Remastered) or (feat. Someone)
    text = re.sub(r"\(.*?\)", "", text)
    # Remove feat. or ft. fragments
    text = re.sub(r"\b(feat|ft)\.?\s+.*", "", text)
    # Strip non-alphanumeric
    return re.sub(r"[^a-z0-9]", "", text)


def matches_title(guess: str, track: Track) -> bool:
    """Check if guess matches track title or any aliases."""
    norm_guess = normalize(guess)
    if not norm_guess:
        return False
    if norm_guess == normalize(track.title):
        return True
    return False


def get_playlists_catalog() -> List[Dict[str, str]]:
    """Returns safe playlist ids and names only. No titles or artists."""
    return [{"id": pl.id, "name": pl.name} for pl in PLAYLISTS]


def get_playlist(playlist_id: str) -> Optional[Playlist]:
    return next((pl for pl in PLAYLISTS if pl.id == playlist_id), None)


def pick_random_track(playlist_id: str, exclude_ids: Optional[set[int]] = None) -> Optional[Track]:
    pl = get_playlist(playlist_id)
    if not pl:
        return None
    exclude = exclude_ids or set()
    available = [t for t in pl.tracks if t.id not in exclude]
    if not available:
        # If all tracks were played in this game, reset pool for this playlist
        available = pl.tracks
    if not available:
        return None
    return random.choice(available)


def get_audio_path(audio_id: str) -> Optional[Path]:
    return AUDIO_STORE.get(audio_id)
