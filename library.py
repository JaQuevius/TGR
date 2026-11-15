"""Soundwich track and playlist library.

Loads playlists and audio paths from folders under ``static/audio`` and
provides title normalization for answer matching.
"""

from __future__ import annotations

import random
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

# Audio folder, resolved next to this file so the server works from any working directory.
AUDIO_DIR = Path(__file__).resolve().parent / "static" / "audio"

# Pseudo-playlist holding MP3s uploaded by players in a lobby (see GameStore.upload_track).
UPLOADS_PLAYLIST_ID = "player-uploads"

# Music Roulette: players' own songs, saved on disk as static/audio/Music roulette/<player name>/<song>.mp3
# Each roulette track's playlist_id is "roulette:<folder name>", so the playlist tells you who uploaded it.
ROULETTE_FOLDER_NAME = "Music roulette"
ROULETTE_DIR = AUDIO_DIR / ROULETTE_FOLDER_NAME
ROULETTE_PLAYLIST_ID = "music-roulette"
ROULETTE_PLAYLIST_PREFIX = "roulette:"


@dataclass
class Track:
    id: Union[int, str]  # int for folder tracks, "upload-..." string for lobby uploads
    title: str
    playlist_id: str


@dataclass
class Playlist:
    id: str
    name: str
    tracks: List[Track]


AUDIO_SUFFIXES = (".mp3", ".wav")

_library_lock = threading.Lock()
_last_track_id = 0


def _next_track_id() -> int:
    global _last_track_id
    _last_track_id += 1
    return _last_track_id


def load_playlists():
    p = AUDIO_DIR
    playlists = []
    audio_store = {}
    current_playlist_id = 0
    for x in sorted(p.iterdir()):
        # The Music Roulette folder holds per-player folders, not a normal playlist
        if x.is_dir() and x.name != ROULETTE_FOLDER_NAME:
            current_playlist_id += 1
            playlist=Playlist(id=f"{x.stem}-{current_playlist_id}", name=x.stem, tracks=[])
            # Process each subdirectory as a playlist
            for i in sorted(x.iterdir()):
                if i.is_file() and i.suffix.lower() in AUDIO_SUFFIXES:
                    # Process each audio file as a track
                    track_id = _next_track_id()
                    playlist.tracks.append(Track(id=track_id, title=i.stem, playlist_id=playlist.id))
                    audio_store[str(track_id)] = i
            playlists.append(playlist)
    return playlists, audio_store

# File paths indexed by string track ID for /audio/<id> requests.
PLAYLISTS, AUDIO_STORE = load_playlists()


# ---------------------------------------------------------------------------
# Music Roulette
# ---------------------------------------------------------------------------

_UNSAFE_PATH_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_path_name(value: str, fallback: str) -> str:
    """Make a player name or song name safe to use as a Windows/macOS/Linux file or folder name."""
    cleaned = _UNSAFE_PATH_CHARS.sub("_", value or "").strip().rstrip(". ")
    cleaned = " ".join(cleaned.split())[:60]
    if not cleaned or cleaned.upper() in _WINDOWS_RESERVED:
        cleaned = f"{fallback}_{cleaned}" if cleaned else fallback
    return cleaned


def roulette_folder_name(player_name: str) -> str:
    """Folder name used for a player's Music Roulette songs."""
    return safe_path_name(player_name, "player")


def roulette_owner_key(value: str) -> str:
    """Case-insensitive key for matching a player name / folder name (folders are case-insensitive on Windows)."""
    return roulette_folder_name(value).casefold()


def roulette_owner_folder(track: Track) -> Optional[str]:
    """Folder (player) name a roulette track belongs to, read from its playlist_id."""
    if not str(track.playlist_id).startswith(ROULETTE_PLAYLIST_PREFIX):
        return None
    return track.playlist_id[len(ROULETTE_PLAYLIST_PREFIX):]


def _register_roulette_file(owner_folder: str, path: Path) -> Track:
    track_id = _next_track_id()
    track = Track(id=track_id, title=path.stem, playlist_id=f"{ROULETTE_PLAYLIST_PREFIX}{owner_folder}")
    AUDIO_STORE[str(track_id)] = path
    ROULETTE_TRACKS.append(track)
    return track


def load_roulette_tracks() -> List[Track]:
    """Load songs players added in earlier sessions (one folder per player)."""
    ROULETTE_DIR.mkdir(parents=True, exist_ok=True)
    for owner_dir in sorted(ROULETTE_DIR.iterdir()):
        if not owner_dir.is_dir():
            continue
        for path in sorted(owner_dir.iterdir()):
            if path.is_file() and path.suffix.lower() in AUDIO_SUFFIXES:
                _register_roulette_file(owner_dir.name, path)
    return ROULETTE_TRACKS


ROULETTE_TRACKS: List[Track] = []
load_roulette_tracks()


def add_roulette_track(player_name: str, filename: str, data: bytes) -> Track:
    """Save an uploaded MP3 into Music roulette/<player name>/ (creating the folder if needed)."""
    with _library_lock:
        owner_key = roulette_owner_key(player_name)
        # Reuse an existing folder for this player (case-insensitive), else create one
        existing = next(
            (d for d in ROULETTE_DIR.iterdir() if d.is_dir() and d.name.casefold() == owner_key),
            None,
        ) if ROULETTE_DIR.exists() else None
        owner_dir = existing or (ROULETTE_DIR / roulette_folder_name(player_name))
        owner_dir.mkdir(parents=True, exist_ok=True)

        stem = safe_path_name(Path(filename).stem, "song")
        path = owner_dir / f"{stem}.mp3"
        counter = 2
        while path.exists():
            path = owner_dir / f"{stem} ({counter}).mp3"
            counter += 1
        path.write_bytes(data)
        return _register_roulette_file(owner_dir.name, path)


def roulette_tracks_for(player_names) -> List[Track]:
    """All roulette tracks uploaded by any of the given players."""
    keys = {roulette_owner_key(name) for name in player_names}
    with _library_lock:
        return [t for t in ROULETTE_TRACKS if (roulette_owner_folder(t) or "").casefold() in keys]

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


def pick_random_track(playlist_id: str, exclude_ids: Optional[set] = None) -> Optional[Track]:
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


def track_source(track: Track, code: str) -> Dict[str, Any]:
    """Client-safe audio source for a track (never includes the title)."""
    if track.playlist_id == UPLOADS_PLAYLIST_ID:
        return {"type": "audio", "id": str(track.id), "url": f"/api/game/{code}/audio/{track.id}"}
    return {"type": "audio", "id": str(track.id)}
