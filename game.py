"""Soundbite Game Logic & Authoritative State Engine.

Thread-safe, dependency-free game state manager supporting both buzzer mode
and race mode with offline-resilient timestamped event resolution.
All times are server epoch milliseconds via an injectable clock.
"""

from __future__ import annotations

import os
import random
import re
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import library

# Timing constants in milliseconds (overridable via environment variables)
COUNTDOWN_MS = int(os.environ.get("SOUNDBITE_COUNTDOWN_MS", "3000"))
ROUND_MS = int(os.environ.get("SOUNDBITE_ROUND_MS", "12000"))
LOCK_MS = int(os.environ.get("SOUNDBITE_LOCK_MS", "1500"))
ANSWER_MS = int(os.environ.get("SOUNDBITE_ANSWER_MS", "8000"))
RECONNECT_GRACE_MS = int(os.environ.get("SOUNDBITE_RECONNECT_GRACE_MS", "5000"))
PRESENCE_MS = int(os.environ.get("SOUNDBITE_PRESENCE_MS", "4000"))
CLOCK_SLACK_MS = int(os.environ.get("SOUNDBITE_CLOCK_SLACK_MS", "300"))
BACKDATE_SLACK_MS = int(os.environ.get("SOUNDBITE_BACKDATE_SLACK_MS", "750"))
LOBBY_IDLE_TIMEOUT_MS = int(os.environ.get("SOUNDBITE_LOBBY_IDLE_MS", "1800000"))  # 30 min
MAX_PLAYERS_PER_LOBBY = 12
MAX_UPLOAD_BYTES = 15 * 1024 * 1024
MAX_UPLOADS_PER_LOBBY = 30
MAX_ROULETTE_SONGS_PER_PLAYER = 30
GAME_MODES = ("buzzer", "race", "roulette")

CODE_PATTERN = re.compile(r"^\d{4}$")


@dataclass
class Player:
    pid: str
    name: str
    token: str
    score: int = 0
    is_host: bool = False
    last_seen: int = 0
    eliminated: bool = False
    buzz_ts: int = 0


@dataclass
class BuzzRecord:
    buzz_id: str
    pid: str
    raw_ts: int
    clamped_ts: int
    received_at: int


@dataclass
class RoundState:
    round_id: str
    mode: str  # "buzzer", "race" or "roulette"
    track: library.Track
    playlist_id: str
    created_at: int
    start_at: int
    buzz_close: int
    phase: str  # "countdown", "open", "locking", "answering", "revealed"
    reverse_audio: bool = False

    # Buzzer-specific fields
    lock_closes_at: Optional[int] = None
    locking_buzzes: List[BuzzRecord] = field(default_factory=list)
    floor_pid: Optional[str] = None
    floor_deadline: Optional[int] = None
    floor_awarded_at: Optional[int] = None
    winner_pid: Optional[str] = None

    # Idempotency caches
    received_buzz_ids: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    received_answer_ids: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    buzzed_pids_this_round: Set[str] = field(default_factory=set)

    # Race-mode specific answers
    race_answers: List[Dict[str, Any]] = field(default_factory=list)

    # Music Roulette: who uploaded the song, and everyone's "who was it?" guesses
    owner_pid: Optional[str] = None
    roulette_guesses: List[Dict[str, Any]] = field(default_factory=list)

    # Event log (last 30 events, each with sequential seq number)
    events: List[Dict[str, Any]] = field(default_factory=list)
    event_seq: int = 0

    def add_event(self, event_type: str, payload: Optional[Dict[str, Any]] = None) -> None:
        self.event_seq += 1
        evt = {"seq": self.event_seq, "type": event_type}
        if payload:
            evt.update(payload)
        self.events.append(evt)
        if len(self.events) > 30:
            self.events.pop(0)


class GameStore:
    """Thread-safe in-memory lobby and round manager with injectable clock."""

    def __init__(self, clock_fn: Optional[Callable[[], float]] = None) -> None:
        # Clock returns server epoch milliseconds as float/int
        self._clock: Callable[[], float] = clock_fn or (lambda: time.time() * 1000.0)
        self._games: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def now(self) -> int:
        return int(self._clock())

    def create_game(self, host_name: str) -> Tuple[str, str, str]:
        """Creates a new lobby. Returns (code, pid, token)."""
        clean_name = self._clean_name(host_name)
        with self._lock:
            now_ms = self.now()
            self._purge_idle_games(now_ms)

            while True:
                code = f"{secrets.randbelow(10000):04d}"
                if code not in self._games:
                    break

            pid = uuid.uuid4().hex[:8]
            token = secrets.token_hex(16)
            host = Player(
                pid=pid,
                name=clean_name,
                token=token,
                score=0,
                is_host=True,
                last_seen=now_ms,
            )

            self._games[code] = {
                "code": code,
                "host_pid": pid,
                "players": {pid: host},
                "tokens": {token: pid},
                "round": None,
                "played_track_ids": set(),
                "uploads": {},
                "lobby_mode": "race",  # mode picked in the host's dropdown, shared with every phone
                "created_at": now_ms,
                "last_active": now_ms,
            }
            return code, pid, token

    def join_game(self, code: str, player_name: str) -> Tuple[str, str, str]:
        """Joins an existing lobby. Allowed at any time. Returns (code, pid, token)."""
        clean_name = self._clean_name(player_name)
        with self._lock:
            now_ms = self.now()
            game = self._games.get(code)
            if not game:
                raise ValueError("That lobby code does not exist.")

            if len(game["players"]) >= MAX_PLAYERS_PER_LOBBY:
                raise ValueError(f"Lobby is full (maximum {MAX_PLAYERS_PER_LOBBY} players).")

            # Unique name resolution: if duplicate, append " 2", " 3", etc.
            final_name = clean_name
            counter = 2
            existing_names = {p.name.lower() for p in game["players"].values()}
            while final_name.lower() in existing_names:
                final_name = f"{clean_name} {counter}"
                counter += 1

            pid = uuid.uuid4().hex[:8]
            token = secrets.token_hex(16)
            player = Player(
                pid=pid,
                name=final_name,
                token=token,
                score=0,
                is_host=False,
                last_seen=now_ms,
            )

            game["players"][pid] = player
            game["tokens"][token] = pid
            game["last_active"] = now_ms

            return code, pid, token

    def get_player_by_token(self, code: str, token: str) -> Player:
        """Authenticate a player via token header."""
        with self._lock:
            game = self._games.get(code)
            if not game:
                raise KeyError("lobby_gone")
            pid = game["tokens"].get(token)
            if not pid or pid not in game["players"]:
                raise PermissionError("bad_token")
            player = game["players"][pid]
            now_ms = self.now()
            player.last_seen = now_ms
            game["last_active"] = now_ms
            return player

    def snapshot(self, code: str, token: str) -> Dict[str, Any]:
        """Returns the full authoritative snapshot for the authenticated player."""
        with self._lock:
            now_ms = self.now()
            game = self._games.get(code)
            if not game:
                raise KeyError("lobby_gone")

            pid = game["tokens"].get(token)
            if not pid or pid not in game["players"]:
                raise PermissionError("bad_token")

            current_player = game["players"][pid]
            current_player.last_seen = now_ms
            game["last_active"] = now_ms

            return self._snapshot_unlocked(game, current_player, now_ms)

    def start_round(
        self,
        code: str,
        token: str,
        mode: str,
        playlist_id: str,
        reverse_audio: bool = False,
    ) -> Dict[str, Any]:
        """Host initiates a new round."""
        with self._lock:
            now_ms = self.now()
            game, host = self._get_game_and_player(code, token)
            if not host.is_host:
                raise ValueError("Only the host can start a round.")

            mode = mode if mode in GAME_MODES else "buzzer"
            game["lobby_mode"] = mode
            owner_pid: Optional[str] = None

            # Validate playlist and pick track
            if mode == "roulette":
                # Music Roulette: a song from the folder of someone currently in this lobby
                if len(game["players"]) < 2:
                    raise ValueError("Music Roulette needs at least 2 players.")
                playlist_id = library.ROULETTE_PLAYLIST_ID
                names = [p.name for p in game["players"].values()]
                pool = library.roulette_tracks_for(names)
                if not pool:
                    raise ValueError("Nobody in this lobby has added songs to Music Roulette yet.")
                available = [t for t in pool if t.id not in game["played_track_ids"]]
                track = random.choice(available) if available else None
                if track:
                    owner_key = (library.roulette_owner_folder(track) or "").casefold()
                    owner_pid = next(
                        (p.pid for p in game["players"].values() if library.roulette_owner_key(p.name) == owner_key),
                        None,
                    )
            elif playlist_id == library.UPLOADS_PLAYLIST_ID:
                available_uploads = [
                    item["track"]
                    for item in game["uploads"].values()
                    if item["track"].id not in game["played_track_ids"]
                ]
                track = random.choice(available_uploads) if available_uploads else None
            else:
                pl = library.get_playlist(playlist_id)
                if not pl:
                    raise ValueError("Invalid playlist selected.")
                track = library.pick_random_track(playlist_id, game["played_track_ids"])
            if not track:
                game["round"] = None
                game["played_track_ids"].clear()
                game["last_active"] = now_ms
                return {
                    "outcome": "game_finished",
                    "snapshot": self._snapshot_unlocked(game, host, now_ms),
                }

            game["played_track_ids"].add(track.id)

            # Reset elimination status for all players
            for p in game["players"].values():
                p.eliminated = False
                p.buzz_ts = 0

            start_at = now_ms + COUNTDOWN_MS
            buzz_close = start_at + ROUND_MS

            new_round = RoundState(
                round_id=uuid.uuid4().hex[:8],
                mode=mode,
                track=track,
                playlist_id=playlist_id,
                created_at=now_ms,
                start_at=start_at,
                buzz_close=buzz_close,
                phase="countdown",
                reverse_audio=bool(reverse_audio),
                owner_pid=owner_pid,
            )
            new_round.add_event("round_started", {
                "mode": new_round.mode,
                "start_at": start_at,
                "buzz_close": buzz_close,
            })

            game["round"] = new_round
            game["last_active"] = now_ms

            return {"outcome": "round_started", "snapshot": self._snapshot_unlocked(game, host, now_ms)}

    def set_lobby_mode(self, code: str, token: str, mode: str) -> Dict[str, Any]:
        """Host changed the Game Mode dropdown; tell every phone (e.g. to show the Music Roulette card)."""
        with self._lock:
            now_ms = self.now()
            game, host = self._get_game_and_player(code, token)
            if not host.is_host:
                raise ValueError("Only the host can change the game mode.")
            if mode not in GAME_MODES:
                raise ValueError("Unknown game mode.")
            game["lobby_mode"] = mode
            game["last_active"] = now_ms
            return {"outcome": "mode_set", "snapshot": self._snapshot_unlocked(game, host, now_ms)}

    def get_game_library(self, code: str, token: str) -> List[Dict[str, str]]:
        with self._lock:
            game, _player = self._get_game_and_player(code, token)
            playlists = library.get_playlists_catalog()
            if game["uploads"]:
                playlists.append({"id": library.UPLOADS_PLAYLIST_ID, "name": "Player uploads"})
            return playlists

    def upload_track(
        self,
        code: str,
        token: str,
        filename: str,
        data: bytes,
        content_type: str,
    ) -> Dict[str, Any]:
        with self._lock:
            now_ms = self.now()
            game, player = self._get_game_and_player(code, token)
            if not filename.lower().endswith(".mp3") or content_type not in ("audio/mpeg", "audio/mp3", "application/octet-stream"):
                raise ValueError("Only MP3 files are supported.")
            if not data:
                raise ValueError("The uploaded MP3 is empty.")
            if len(data) > MAX_UPLOAD_BYTES:
                raise ValueError("Each MP3 must be 15 MB or smaller.")
            if len(game["uploads"]) >= MAX_UPLOADS_PER_LOBBY:
                raise ValueError("This lobby has reached its 30-file upload limit.")

            track_id = f"upload-{uuid.uuid4().hex[:12]}"
            title = os.path.splitext(os.path.basename(filename))[0].replace("_", " ").replace("-", " ").strip()
            title = " ".join(title.split()).title() or "Uploaded track"
            track = library.Track(
                id=track_id,
                title=title,
                playlist_id=library.UPLOADS_PLAYLIST_ID,
            )
            game["uploads"][track_id] = {
                "track": track,
                "data": data,
                "content_type": "audio/mpeg",
                "owner": player.pid,
            }
            game["last_active"] = now_ms
            return {"track_id": track_id, "title": title, "upload_count": len(game["uploads"])}

    def upload_roulette_track(
        self,
        code: str,
        token: str,
        filename: str,
        data: bytes,
        content_type: str,
    ) -> Dict[str, Any]:
        """Saves a player's MP3 to static/audio/Music roulette/<player name>/ for Music Roulette."""
        with self._lock:
            now_ms = self.now()
            game, player = self._get_game_and_player(code, token)
            if not filename.lower().endswith(".mp3") or content_type not in ("audio/mpeg", "audio/mp3", "application/octet-stream"):
                raise ValueError("Only MP3 files are supported.")
            if not data:
                raise ValueError("The uploaded MP3 is empty.")
            if len(data) > MAX_UPLOAD_BYTES:
                raise ValueError("Each MP3 must be 15 MB or smaller.")
            if len(library.roulette_tracks_for([player.name])) >= MAX_ROULETTE_SONGS_PER_PLAYER:
                raise ValueError(f"You already have {MAX_ROULETTE_SONGS_PER_PLAYER} songs in Music Roulette.")

            track = library.add_roulette_track(player.name, filename, data)
            player.last_seen = now_ms
            game["last_active"] = now_ms
            return {
                "title": track.title,
                "my_count": len(library.roulette_tracks_for([player.name])),
                "lobby_count": len(library.roulette_tracks_for([p.name for p in game["players"].values()])),
            }

    def get_uploaded_audio(self, code: str, token: str, track_id: str) -> Tuple[bytes, str]:
        with self._lock:
            game, _player = self._get_game_and_player(code, token)
            upload = game["uploads"].get(track_id)
            if not upload:
                raise KeyError("Audio clip not found.")
            return upload["data"], upload["content_type"]

    def submit_buzz(self, code: str, token: str, round_id: str, buzz_id: str, client_ts: int) -> Dict[str, Any]:
        """Player attempts to buzz in. Returns outcome and fresh snapshot."""
        with self._lock:
            now_ms = self.now()
            game, player = self._get_game_and_player(code, token)
            player.last_seen = now_ms
            game["last_active"] = now_ms

            self._advance_round(game, now_ms)
            rnd: Optional[RoundState] = game["round"]

            if not rnd or rnd.round_id != round_id:
                return {
                    "outcome": {"status": "rejected", "reason": "No active round with that ID"},
                    "snapshot": self._snapshot_unlocked(game, player, now_ms),
                }

            # Idempotency check: if already processed this buzz_id, return cached outcome
            if buzz_id in rnd.received_buzz_ids:
                return {
                    "outcome": rnd.received_buzz_ids[buzz_id],
                    "snapshot": self._snapshot_unlocked(game, player, now_ms),
                }

            # Check elimination
            if player.eliminated:
                outcome = {"status": "rejected", "reason": "You are eliminated for this round"}
                rnd.received_buzz_ids[buzz_id] = outcome
                return {"outcome": outcome, "snapshot": self._snapshot_unlocked(game, player, now_ms)}

            # One buzz per player per race/locking window
            if player.pid in rnd.buzzed_pids_this_round:
                outcome = {"status": "rejected", "reason": "You have already buzzed this round"}
                rnd.received_buzz_ids[buzz_id] = outcome
                return {"outcome": outcome, "snapshot": self._snapshot_unlocked(game, player, now_ms)}

            # Timestamp clamping:
            # 1. Cannot be in the future beyond receipt time
            # 2. Cannot be backdated before player's last contact minus BACKDATE_SLACK_MS
            clamped_ts = min(now_ms, client_ts)
            clamped_ts = max(player.last_seen - BACKDATE_SLACK_MS, clamped_ts)

            # Check if too early
            if clamped_ts < rnd.start_at - CLOCK_SLACK_MS:
                outcome = {"status": "rejected", "reason": "too_early"}
                rnd.received_buzz_ids[buzz_id] = outcome
                return {"outcome": outcome, "snapshot": self._snapshot_unlocked(game, player, now_ms)}

            # Clamp slightly-early buzz within CLOCK_SLACK_MS to start_at
            if clamped_ts < rnd.start_at:
                clamped_ts = rnd.start_at

            # Check if after round close
            if clamped_ts > rnd.buzz_close:
                outcome = {"status": "rejected", "reason": "round_over"}
                rnd.received_buzz_ids[buzz_id] = outcome
                return {"outcome": outcome, "snapshot": self._snapshot_unlocked(game, player, now_ms)}

            # Check phase
            if rnd.phase in ("answering", "revealed"):
                outcome = {"status": "late", "reason": "Floor already awarded or round revealed"}
                rnd.received_buzz_ids[buzz_id] = outcome
                return {"outcome": outcome, "snapshot": self._snapshot_unlocked(game, player, now_ms)}

            if rnd.phase == "locking":
                if now_ms > (rnd.lock_closes_at or 0):
                    outcome = {"status": "late", "reason": "Locking window closed, floor awarded"}
                    rnd.received_buzz_ids[buzz_id] = outcome
                    return {"outcome": outcome, "snapshot": self._snapshot_unlocked(game, player, now_ms)}

                # In locking window: add to race
                buzz_rec = BuzzRecord(buzz_id=buzz_id, pid=player.pid, raw_ts=client_ts, clamped_ts=clamped_ts, received_at=now_ms)
                rnd.locking_buzzes.append(buzz_rec)
                rnd.buzzed_pids_this_round.add(player.pid)
                player.buzz_ts = clamped_ts

                outcome = {"status": "locking", "lock_closes_at": rnd.lock_closes_at}
                rnd.received_buzz_ids[buzz_id] = outcome
                return {"outcome": outcome, "snapshot": self._snapshot_unlocked(game, player, now_ms)}

            if rnd.phase == "open":
                # First valid buzz! Start locking window
                rnd.phase = "locking"
                rnd.lock_closes_at = now_ms + LOCK_MS
                buzz_rec = BuzzRecord(buzz_id=buzz_id, pid=player.pid, raw_ts=client_ts, clamped_ts=clamped_ts, received_at=now_ms)
                rnd.locking_buzzes.append(buzz_rec)
                rnd.buzzed_pids_this_round.add(player.pid)
                player.buzz_ts = clamped_ts

                rnd.add_event("locking_started", {"lock_closes_at": rnd.lock_closes_at})

                outcome = {"status": "locking", "lock_closes_at": rnd.lock_closes_at}
                rnd.received_buzz_ids[buzz_id] = outcome
                return {"outcome": outcome, "snapshot": self._snapshot_unlocked(game, player, now_ms)}

            # If countdown or unexpected
            outcome = {"status": "rejected", "reason": f"Cannot buzz in phase {rnd.phase}"}
            rnd.received_buzz_ids[buzz_id] = outcome
            return {"outcome": outcome, "snapshot": self._snapshot_unlocked(game, player, now_ms)}

    def submit_answer(
        self,
        code: str,
        token: str,
        round_id: str,
        answer_id: str,
        answer: str,
        client_ts: int,
    ) -> Dict[str, Any]:
        """Submits an answer in buzzer mode or race mode."""
        with self._lock:
            now_ms = self.now()
            game, player = self._get_game_and_player(code, token)
            player.last_seen = now_ms
            game["last_active"] = now_ms

            self._advance_round(game, now_ms)
            rnd: Optional[RoundState] = game["round"]

            if not rnd or rnd.round_id != round_id:
                return {
                    "outcome": {"status": "rejected", "reason": "No active round with that ID"},
                    "snapshot": self._snapshot_unlocked(game, player, now_ms),
                }

            # Idempotency check
            if answer_id in rnd.received_answer_ids:
                return {
                    "outcome": rnd.received_answer_ids[answer_id],
                    "snapshot": self._snapshot_unlocked(game, player, now_ms),
                }

            # -----------------------------------------------------------------
            # Music Roulette: the answer is the pid of the player you think uploaded the song
            # -----------------------------------------------------------------
            if rnd.mode == "roulette":
                def reject(reason: str) -> Dict[str, Any]:
                    outcome = {"status": "rejected", "reason": reason, "roulette": True}
                    rnd.received_answer_ids[answer_id] = outcome
                    return {"outcome": outcome, "snapshot": self._snapshot_unlocked(game, player, now_ms)}

                if rnd.phase != "open":
                    return reject("Guessing is not open right now")
                if player.pid == rnd.owner_pid:
                    return reject("This is your song - sit back and watch them guess!")
                if any(g["pid"] == player.pid for g in rnd.roulette_guesses):
                    return reject("You already locked in a guess this round")
                guessed = game["players"].get(answer.strip())
                if not guessed:
                    return reject("Pick one of the players in the lobby")

                is_correct = guessed.pid == rnd.owner_pid
                points = 0
                if is_correct:
                    placement = sum(1 for g in rnd.roulette_guesses if g["correct"])
                    points = max(40, 100 - placement * 20)
                    player.score += points
                    if not rnd.winner_pid:
                        rnd.winner_pid = player.pid
                rnd.roulette_guesses.append({
                    "pid": player.pid,
                    "name": player.name,
                    "guess_pid": guessed.pid,
                    "guess_name": guessed.name,
                    "correct": is_correct,
                    "points": points,
                })
                # Don't reveal right/wrong to others until the round ends
                rnd.add_event("roulette_guess", {"name": player.name})

                guessers = [p for p in game["players"].values() if p.pid != rnd.owner_pid]
                if len(rnd.roulette_guesses) >= len(guessers):
                    rnd.phase = "revealed"
                    winner = game["players"].get(rnd.winner_pid) if rnd.winner_pid else None
                    rnd.add_event("round_revealed", {"winner": winner.name if winner else None})

                outcome = {"status": "correct" if is_correct else "incorrect", "points": points, "roulette": True}
                rnd.received_answer_ids[answer_id] = outcome
                return {"outcome": outcome, "snapshot": self._snapshot_unlocked(game, player, now_ms)}

            # -----------------------------------------------------------------
            # Race Mode Handling
            # -----------------------------------------------------------------
            if rnd.mode == "race":
                if rnd.phase != "open":
                    outcome = {"status": "rejected", "reason": "Race round is not currently open"}
                    rnd.received_answer_ids[answer_id] = outcome
                    return {"outcome": outcome, "snapshot": self._snapshot_unlocked(game, player, now_ms)}

                if any(ans["pid"] == player.pid for ans in rnd.race_answers):
                    outcome = {"status": "rejected", "reason": "You already answered this round"}
                    rnd.received_answer_ids[answer_id] = outcome
                    return {"outcome": outcome, "snapshot": self._snapshot_unlocked(game, player, now_ms)}

                is_correct = library.matches_title(answer, rnd.track)
                if not is_correct:
                    outcome = {"status": "incorrect", "reason": "Not quite - keep trying"}
                    rnd.received_answer_ids[answer_id] = outcome
                    return {"outcome": outcome, "snapshot": self._snapshot_unlocked(game, player, now_ms)}

                placement = len(rnd.race_answers)
                points = max(40, 100 - placement * 20)
                player.score += points
                result_entry = {
                    "pid": player.pid,
                    "name": player.name,
                    "points": points,
                }
                rnd.race_answers.append(result_entry)
                rnd.add_event("race_correct", {"pid": player.pid, "name": player.name, "points": points})

                if not rnd.winner_pid:
                    rnd.winner_pid = player.pid

                # If all players answered correctly, finish
                if len(rnd.race_answers) >= len(game["players"]):
                    rnd.phase = "revealed"
                    rnd.add_event("round_revealed", {"winner": player.name})

                outcome = {"status": "correct", "points": points}
                rnd.received_answer_ids[answer_id] = outcome
                return {"outcome": outcome, "snapshot": self._snapshot_unlocked(game, player, now_ms)}

            # -----------------------------------------------------------------
            # Buzzer Mode Handling
            # -----------------------------------------------------------------
            if rnd.phase != "answering" or rnd.floor_pid != player.pid:
                outcome = {"status": "rejected", "reason": "You do not hold the floor"}
                rnd.received_answer_ids[answer_id] = outcome
                return {"outcome": outcome, "snapshot": self._snapshot_unlocked(game, player, now_ms)}

            # Stamp clamping: stamp >= holder's buzz ts and <= receipt_now
            clamped_ts = max(player.buzz_ts, min(now_ms, client_ts))

            # Deadline validation
            deadline = rnd.floor_deadline or 0
            is_connected = (now_ms - player.last_seen) <= PRESENCE_MS
            max_receipt_window = deadline + (500 if is_connected else RECONNECT_GRACE_MS)

            # An answer counts if stamped <= deadline + 250ms and received within max_receipt_window
            if clamped_ts > (deadline + 250) or now_ms > max_receipt_window:
                outcome = {"status": "timeout", "reason": "Answer submitted after deadline"}
                rnd.received_answer_ids[answer_id] = outcome
                # Process timeout under normal rules
                self._handle_floor_timeout(game, rnd, player, is_connected=is_connected, now_ms=now_ms)
                return {"outcome": outcome, "snapshot": self._snapshot_unlocked(game, player, now_ms)}

            is_correct = library.matches_title(answer, rnd.track)
            if is_correct:
                points = 100
                player.score += points
                rnd.winner_pid = player.pid
                rnd.phase = "revealed"
                rnd.add_event("answer_correct", {"pid": player.pid, "name": player.name, "points": points})
                rnd.add_event("round_revealed", {"winner": player.name})

                outcome = {"status": "correct", "points": points}
                rnd.received_answer_ids[answer_id] = outcome
                return {"outcome": outcome, "snapshot": self._snapshot_unlocked(game, player, now_ms)}
            else:
                # Wrong answer: -50 and eliminated
                points = -50
                player.score += points
                player.eliminated = True
                rnd.add_event("answer_wrong", {"pid": player.pid, "name": player.name, "points": points})

                # Can floor reopen?
                eligible_players = [p for p in game["players"].values() if not p.eliminated]
                if now_ms < rnd.buzz_close and eligible_players:
                    rnd.phase = "open"
                    rnd.floor_pid = None
                    rnd.floor_deadline = None
                    rnd.buzzed_pids_this_round.clear()  # reset buzz eligibility for remaining eligible players
                    rnd.add_event("floor_reopened", {})
                else:
                    rnd.phase = "revealed"
                    rnd.add_event("round_revealed", {"winner": None})

                outcome = {"status": "incorrect", "points": points, "eliminated": True}
                rnd.received_answer_ids[answer_id] = outcome
                return {"outcome": outcome, "snapshot": self._snapshot_unlocked(game, player, now_ms)}

    def finish_round(self, code: str, token: str) -> Dict[str, Any]:
        """Host forces current round to reveal/finish."""
        with self._lock:
            now_ms = self.now()
            game, host = self._get_game_and_player(code, token)
            if not host.is_host:
                raise ValueError("Only the host can finish a round.")

            rnd: Optional[RoundState] = game["round"]
            if rnd:
                if rnd.phase != "revealed":
                    rnd.phase = "revealed"
                    rnd.add_event("round_revealed", {"winner": None})
                game["round"] = None
                game["last_active"] = now_ms

            return {"outcome": "round_finished", "snapshot": self._snapshot_unlocked(game, host, now_ms)}

    def leave_game(self, code: str, token: str) -> Dict[str, Any]:
        """Player leaves lobby cleanly."""
        with self._lock:
            now_ms = self.now()
            game, player = self._get_game_and_player(code, token)
            player.last_seen = 0  # immediately marks disconnected
            game["last_active"] = now_ms
            return {"outcome": "left", "ok": True}

    # -------------------------------------------------------------------------
    # Internal Helpers & State Machine Progression
    # -------------------------------------------------------------------------

    def _advance_round(self, game: Dict[str, Any], now_ms: int) -> None:
        """Deterministically advances the round state machine to `now_ms`."""
        rnd: Optional[RoundState] = game.get("round")
        if not rnd:
            return

        # 1. Countdown -> Open
        if rnd.phase == "countdown" and now_ms >= rnd.start_at:
            rnd.phase = "open"
            rnd.add_event("round_open", {})

        # 2. Locking -> Answering
        if rnd.phase == "locking" and now_ms >= (rnd.lock_closes_at or 0):
            if rnd.locking_buzzes:
                # Winner is earliest stamped ts, tie breaker earlier received_at
                rnd.locking_buzzes.sort(key=lambda b: (b.clamped_ts, b.received_at))
                winner = rnd.locking_buzzes[0]
                rnd.floor_pid = winner.pid
                rnd.floor_awarded_at = rnd.lock_closes_at
                rnd.floor_deadline = (rnd.lock_closes_at or now_ms) + ANSWER_MS
                rnd.phase = "answering"
                rnd.locking_buzzes.clear()

                winner_player = game["players"].get(winner.pid)
                winner_name = winner_player.name if winner_player else "Player"
                rnd.add_event("floor_awarded", {
                    "pid": winner.pid,
                    "name": winner_name,
                    "deadline": rnd.floor_deadline,
                })
            else:
                # Revert to open if round still active, else reveal
                if now_ms < rnd.buzz_close:
                    rnd.phase = "open"
                    rnd.add_event("floor_reopened", {})
                else:
                    rnd.phase = "revealed"
                    rnd.add_event("round_revealed", {"winner": None})

        # 3. Answering Timeout
        if rnd.phase == "answering" and rnd.floor_deadline:
            holder = game["players"].get(rnd.floor_pid) if rnd.floor_pid else None
            is_connected = ((now_ms - holder.last_seen) <= PRESENCE_MS) if holder else False
            timeout_cutoff = rnd.floor_deadline + (500 if is_connected else RECONNECT_GRACE_MS)

            if now_ms > timeout_cutoff:
                self._handle_floor_timeout(game, rnd, holder, is_connected=is_connected, now_ms=now_ms)

        # 4. Open -> Straggler Wait / Revealed
        if rnd.phase == "open" and now_ms >= rnd.buzz_close:
            # If nobody buzzed, wait LOCK_MS for stamped-in-time stragglers
            if now_ms >= rnd.buzz_close + LOCK_MS:
                rnd.phase = "revealed"
                rnd.add_event("round_revealed", {"winner": None})

    def _handle_floor_timeout(
        self,
        game: Dict[str, Any],
        rnd: RoundState,
        holder: Optional[Player],
        is_connected: bool,
        now_ms: int,
    ) -> None:
        """Resolves answering floor timeout for connected vs disconnected holder."""
        if holder:
            holder.eliminated = True
            if is_connected:
                # Connected holder: penalized -50
                holder.score -= 50
                rnd.add_event("answer_timeout", {"pid": holder.pid, "name": holder.name, "points": -50})
            else:
                # Disconnected holder: eliminated with no penalty
                rnd.add_event("disconnected_timeout", {"pid": holder.pid, "name": holder.name, "points": 0})

        # Can floor reopen?
        eligible = [p for p in game["players"].values() if not p.eliminated]
        if now_ms < rnd.buzz_close and eligible:
            rnd.phase = "open"
            rnd.floor_pid = None
            rnd.floor_deadline = None
            rnd.buzzed_pids_this_round.clear()
            rnd.add_event("floor_reopened", {})
        else:
            rnd.phase = "revealed"
            rnd.add_event("round_revealed", {"winner": None})

    def _snapshot_unlocked(self, game: Dict[str, Any], player: Player, now_ms: int) -> Dict[str, Any]:
        """Internal snapshot generator without re-acquiring lock."""
        self._advance_round(game, now_ms)

        players_data = []
        for p in sorted(game["players"].values(), key=lambda x: (-x.score, x.name.lower())):
            is_connected = (now_ms - p.last_seen) <= PRESENCE_MS
            players_data.append({
                "pid": p.pid,
                "name": p.name,
                "score": p.score,
                "connected": is_connected,
                "eliminated": p.eliminated,
            })

        round_data = None
        round_state: Optional[RoundState] = game["round"]
        if round_state:
            round_data = {
                "round_id": round_state.round_id,
                "mode": round_state.mode,
                "phase": round_state.phase,
                "start_at": round_state.start_at,
                "buzz_close": round_state.buzz_close,
                "playlist_id": round_state.playlist_id,
                "reverse_audio": round_state.reverse_audio,
                "source": library.track_source(round_state.track, game["code"]),
                "events": list(round_state.events),
                "winner": round_state.winner_pid,
            }

            if round_state.phase == "locking":
                round_data["lock_closes_at"] = round_state.lock_closes_at

            if round_state.phase == "answering" and round_state.floor_pid:
                holder = game["players"].get(round_state.floor_pid)
                holder_connected = ((now_ms - holder.last_seen) <= PRESENCE_MS) if holder else False
                round_data["floor"] = {
                    "pid": round_state.floor_pid,
                    "name": holder.name if holder else "Unknown",
                    "deadline": round_state.floor_deadline,
                    "holder_connected": holder_connected,
                }

            if round_state.mode == "race":
                round_data["race_answers"] = [
                    {"name": item["name"], "points": item["points"]}
                    for item in round_state.race_answers
                ]

            if round_state.mode == "roulette":
                my_guess = next((g for g in round_state.roulette_guesses if g["pid"] == player.pid), None)
                round_data["roulette"] = {
                    # Everyone in the lobby is a possible answer (clients hide themselves)
                    "choices": [{"pid": p.pid, "name": p.name} for p in game["players"].values()],
                    "guessed": [g["name"] for g in round_state.roulette_guesses],
                    "you_uploaded": player.pid == round_state.owner_pid,
                    "my_guess": my_guess["guess_pid"] if my_guess else None,
                }

            if round_state.phase == "revealed":
                winner_name = None
                if round_state.winner_pid and round_state.winner_pid in game["players"]:
                    winner_name = game["players"][round_state.winner_pid].name
                round_data["reveal"] = {
                    "title": round_state.track.title,
                    "winner": winner_name,
                }
                if round_state.mode == "roulette":
                    owner = game["players"].get(round_state.owner_pid) if round_state.owner_pid else None
                    round_data["reveal"]["owner"] = owner.name if owner else library.roulette_owner_folder(round_state.track)
                    round_data["reveal"]["guesses"] = [
                        {"name": g["name"], "guess_name": g["guess_name"], "correct": g["correct"], "points": g["points"]}
                        for g in round_state.roulette_guesses
                    ]

        lobby_names = [p.name for p in game["players"].values()]
        return {
            "server_time": now_ms,
            "code": game["code"],
            "upload_count": len(game["uploads"]),
            "lobby_mode": game.get("lobby_mode", "race"),
            # Music Roulette songs saved by players in this lobby (all, and just mine)
            "roulette_count": len(library.roulette_tracks_for(lobby_names)),
            "my_roulette_count": len(library.roulette_tracks_for([player.name])),
            "players": players_data,
            "me": {
                "pid": player.pid,
                "host": player.is_host,
                "name": player.name,
            },
            "round": round_data,
        }

    def _get_game_and_player(self, code: str, token: str) -> Tuple[Dict[str, Any], Player]:
        game = self._games.get(code)
        if not game:
            raise KeyError("lobby_gone")
        pid = game["tokens"].get(token)
        if not pid or pid not in game["players"]:
            raise PermissionError("bad_token")
        return game, game["players"][pid]

    def _purge_idle_games(self, now_ms: int) -> None:
        idle_codes = [
            code for code, g in self._games.items()
            if (now_ms - g["last_active"]) > LOBBY_IDLE_TIMEOUT_MS
        ]
        for code in idle_codes:
            del self._games[code]

    @staticmethod
    def _clean_name(value: Any) -> str:
        name = str(value or "").strip()
        if not name:
            raise ValueError("Enter a name first.")
        return name[:24]
