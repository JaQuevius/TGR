"""Soundbite Track Library and Audio Synthesis.

Holds server-side curated playlists, in-memory synthesized WAV clips for the
offline "Bread demo", track metadata, aliases, and title normalization.
No track titles or answers are exposed to clients before reveal.
"""

from __future__ import annotations

import io
import random
import re
import wave
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


def generate_wav_bytes(frequencies: tuple[int, ...], duration_sec: float = 6.0) -> bytes:
    """Generate raw in-memory WAV bytes with harmonic tones for demo tracks."""
    sample_rate = 8000
    total_samples = int(sample_rate * duration_sec)
    frames = bytearray()

    for sample in range(total_samples):
        # Blend frequencies with a subtle decay envelope
        val = 0.0
        for freq in frequencies:
            period = max(1, sample_rate // freq)
            val += (sample % period) / period - 0.5
        val /= len(frequencies)
        int_sample = int(9000 * val)
        frames.extend(int_sample.to_bytes(2, "little", signed=True))

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as clip:
        clip.setnchannels(1)
        clip.setsampwidth(2)
        clip.setframerate(sample_rate)
        clip.writeframes(frames)
    return buffer.getvalue()


@dataclass
class Track:
    id: str
    title: str
    artist: str
    aliases: List[str] = field(default_factory=list)
    source: Dict[str, Any] = field(default_factory=dict)
    audio_bytes: Optional[bytes] = None


@dataclass
class Playlist:
    id: str
    name: str
    tracks: List[Track]


# In-memory synthesized audio store
AUDIO_STORE: Dict[str, bytes] = {}


def _init_audio_store() -> None:
    AUDIO_STORE["sunrise"] = generate_wav_bytes((392, 523, 659))
    AUDIO_STORE["midnight"] = generate_wav_bytes((330, 440, 554))
    AUDIO_STORE["jams"] = generate_wav_bytes((262, 330, 392))


_init_audio_store()


PLAYLISTS: List[Playlist] = [
    Playlist(
        id="bread-demo",
        name="Bread demo (offline tones)",
        tracks=[
            Track(
                id="bread-1",
                title="Sunrise on Sourdough",
                artist="The Loafers",
                aliases=["Sunrise", "Sourdough"],
                source={"type": "audio", "id": "sunrise"},
                audio_bytes=AUDIO_STORE["sunrise"],
            ),
            Track(
                id="bread-2",
                title="Midnight at the Bakery",
                artist="Crumb Club",
                aliases=["Midnight", "Bakery"],
                source={"type": "audio", "id": "midnight"},
                audio_bytes=AUDIO_STORE["midnight"],
            ),
            Track(
                id="bread-3",
                title="Jam on Toast",
                artist="Butter & Beats",
                aliases=["Jam", "Toast"],
                source={"type": "audio", "id": "jams"},
                audio_bytes=AUDIO_STORE["jams"],
            ),
        ],
    ),
    Playlist(
        id="local-mp3",
        name="My MP3s",
        tracks=[
            Track(
                id="somewhere_only_we_know",
                title="Somewhere Only We Know",
                artist="Local MP3",
                aliases=["Somewhere Only We Know"],
                source={"type": "audio", "id": "somewhere_only_we_know"},
            ),
            Track(
                id="this_side_of_paradise",
                title="This Side of Paradise",
                artist="Local MP3",
                aliases=["This Side Of Paradise"],
                source={"type": "audio", "id": "this_side_of_paradise"},
            ),
            Track(
                id="weston_super_mare",
                title="Weston Super Mare",
                artist="Local MP3",
                aliases=["Weston-super-Mare", "Weston Super Mare"],
                source={"type": "audio", "id": "weston_super_mare"},
            ),
            Track(
                id="despacito",
                title="Despacito",
                artist="Luis Fonsi ft. Daddy Yankee",
                aliases=["Despacito"],
                source={"type": "audio", "id": "despacito"},
            ),
        ],
    ),
    Playlist(
        id="90s-throwbacks",
        name="90s throwbacks",
        tracks=[
            Track(
                id="90s-1",
                title="...Baby One More Time",
                artist="Britney Spears",
                aliases=["Baby One More Time", "Hit Me Baby One More Time"],
                source={"type": "youtube", "videoId": "C-u5WLJ9Yk4", "start": 0, "end": 10},
            ),
            Track(
                id="90s-2",
                title="Smells Like Teen Spirit",
                artist="Nirvana",
                aliases=["Teen Spirit"],
                source={"type": "youtube", "videoId": "hTWKbfoikeg", "start": 0, "end": 10},
            ),
            Track(
                id="90s-3",
                title="Wannabe",
                artist="Spice Girls",
                aliases=["Wanna Be", "Tell Me What You Want"],
                source={"type": "youtube", "videoId": "gJLIiF15wjQ", "start": 0, "end": 10},
            ),
            Track(
                id="90s-4",
                title="Waterfalls",
                artist="TLC",
                aliases=["Dont Go Chasing Waterfalls"],
                source={"type": "youtube", "videoId": "8WEtxJ4-sh4", "start": 0, "end": 10},
            ),
        ],
    ),
    Playlist(
        id="2000s-hits",
        name="2000s hits",
        tracks=[
            Track(
                id="00s-1",
                title="Crazy in Love",
                artist="Beyoncé",
                aliases=["Crazy In Love"],
                source={"type": "youtube", "videoId": "ViwtNLUqkMY", "start": 0, "end": 10},
            ),
            Track(
                id="00s-2",
                title="Mr. Brightside",
                artist="The Killers",
                aliases=["Mr Brightside", "Mister Brightside"],
                source={"type": "youtube", "videoId": "gGdGFtwoWP0", "start": 0, "end": 10},
            ),
            Track(
                id="00s-3",
                title="Since U Been Gone",
                artist="Kelly Clarkson",
                aliases=["Since You Been Gone", "Since Youve Been Gone"],
                source={"type": "youtube", "videoId": "R7UrFYvl5TE", "start": 0, "end": 10},
            ),
            Track(
                id="00s-4",
                title="Yeah!",
                artist="Usher ft. Lil Jon & Ludacris",
                aliases=["Yeah"],
                source={"type": "youtube", "videoId": "GxBSyx85Kp8", "start": 0, "end": 10},
            ),
        ],
    ),
    Playlist(
        id="2010s-anthems",
        name="2010s anthems",
        tracks=[
            Track(
                id="10s-1",
                title="Uptown Funk",
                artist="Mark Ronson ft. Bruno Mars",
                aliases=["Uptown Funk You Up"],
                source={"type": "youtube", "videoId": "OPf0YbXqDm0", "start": 0, "end": 10},
            ),
            Track(
                id="10s-2",
                title="Rolling in the Deep",
                artist="Adele",
                aliases=["Rolling In The Deep"],
                source={"type": "youtube", "videoId": "rYEDA3JcQqw", "start": 0, "end": 10},
            ),
            Track(
                id="10s-3",
                title="Blinding Lights",
                artist="The Weeknd",
                aliases=["Blinding Lights"],
                source={"type": "youtube", "videoId": "4NRXx6U8ABQ", "start": 0, "end": 10},
            ),
            Track(
                id="10s-4",
                title="Royals",
                artist="Lorde",
                aliases=["We Will Never Be Royals"],
                source={"type": "youtube", "videoId": "nlcIKh6sBtc", "start": 0, "end": 10},
            ),
        ],
    ),
    Playlist(
        id="edm-party",
        name="EDM party",
        tracks=[
            Track(
                id="edm-1",
                title="Levels",
                artist="Avicii",
                aliases=["Good Feeling"],
                source={"type": "youtube", "videoId": "_ovdm2yX4MA", "start": 0, "end": 10},
            ),
            Track(
                id="edm-2",
                title="Titanium",
                artist="David Guetta ft. Sia",
                aliases=["I Am Titanium"],
                source={"type": "youtube", "videoId": "JRfuAukYTKg", "start": 0, "end": 10},
            ),
            Track(
                id="edm-3",
                title="Don't You Worry Child",
                artist="Swedish House Mafia",
                aliases=["Dont You Worry Child", "See Heaven Got A Plan For You"],
                source={"type": "youtube", "videoId": "1y6smkh6c-0", "start": 0, "end": 10},
            ),
            Track(
                id="edm-4",
                title="Wake Me Up",
                artist="Avicii",
                aliases=["Wake Me Up When Its All Over"],
                source={"type": "youtube", "videoId": "IcrbM1l_BoI", "start": 0, "end": 10},
            ),
        ],
    ),
    Playlist(
        id="popular-youtube",
        name="Popular YouTube hits",
        tracks=[
            Track(
                id="popular-1",
                title="Shape of You",
                artist="Ed Sheeran",
                aliases=["Shape Of You"],
                source={"type": "youtube", "videoId": "JGwWNGJDvW8", "start": 0, "end": 10},
            ),
            Track(
                id="popular-2",
                title="Despacito",
                artist="Luis Fonsi ft. Daddy Yankee",
                aliases=["Despacito"],
                source={"type": "youtube", "videoId": "kJQP7kiw5Fk", "start": 0, "end": 10},
            ),
            Track(
                id="popular-3",
                title="Gangnam Style",
                artist="PSY",
                aliases=["Gangnam Style", "Oppa Gangnam Style"],
                source={"type": "youtube", "videoId": "9bZkp7q19f0", "start": 0, "end": 10},
            ),
            Track(
                id="popular-4",
                title="Hello",
                artist="Adele",
                aliases=["Hello Adele"],
                source={"type": "youtube", "videoId": "YQHsXMglC9A", "start": 0, "end": 10},
            ),
            Track(
                id="popular-5",
                title="Sugar",
                artist="Maroon 5",
                aliases=["Sugar Maroon 5"],
                source={"type": "youtube", "videoId": "09R8_2nJtjg", "start": 0, "end": 10},
            ),
            Track(
                id="popular-6",
                title="Faded",
                artist="Alan Walker",
                aliases=["Faded Alan Walker"],
                source={"type": "youtube", "videoId": "60ItHLz5WEA", "start": 0, "end": 10},
            ),
        ],
    ),
]


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
    for alias in track.aliases:
        if norm_guess == normalize(alias):
            return True
    return False


def get_playlists_catalog() -> List[Dict[str, str]]:
    """Returns safe playlist ids and names only. No titles or artists."""
    return [{"id": pl.id, "name": pl.name} for pl in PLAYLISTS]


def get_playlist(playlist_id: str) -> Optional[Playlist]:
    return next((pl for pl in PLAYLISTS if pl.id == playlist_id), None)


def pick_random_track(playlist_id: str, exclude_ids: Optional[set[str]] = None) -> Optional[Track]:
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


def get_audio_bytes(audio_id: str) -> Optional[bytes]:
    return AUDIO_STORE.get(audio_id)
