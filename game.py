"""Soundbite Game Logic & Authoritative State Engine.

Thread-safe, dependency-free game state manager supporting both buzzer mode
and race mode with offline-resilient timestamped event resolution.
All times are server epoch milliseconds via an injectable clock.
"""

from __future__ import annotations

import os
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
    mode: str  # "buzzer" or "race"
    track: library.Track
    playlist_id: str
    created_at: int
    start_at: int
    buzz_close: int
    phase: str  # "countdown", "open", "locking", "answering", "revealed"

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

            # Advance state machine before snapshot
            self._advance_round(game, now_ms)

            # Public players list (NO secret tokens leaked!)
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
                    "source": {"type": "audio", "id": str(round_state.track.id)},
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
                    # Safe race answers without exposing player IDs or answers
                    round_data["race_answers"] = [
                        {"name": item["name"], "points": item["points"]}
                        for item in round_state.race_answers
                    ]

                # Reveal only when phase is "revealed"
                if round_state.phase == "revealed":
                    winner_name = None
                    if round_state.winner_pid and round_state.winner_pid in game["players"]:
                        winner_name = game["players"][round_state.winner_pid].name
                    round_data["reveal"] = {
                        "title": round_state.track.title,
                        "winner": winner_name,
                    }

            return {
                "server_time": now_ms,
                "code": code,
                "players": players_data,
                "me": {
                    "pid": current_player.pid,
                    "host": current_player.is_host,
                    "name": current_player.name,
                },
                "round": round_data,
            }

    def start_round(self, code: str, token: str, mode: str, playlist_id: str) -> Dict[str, Any]:
        """Host initiates a new round."""
        with self._lock:
            now_ms = self.now()
            game, host = self._get_game_and_player(code, token)
            if not host.is_host:
                raise ValueError("Only the host can start a round.")

            # Validate playlist and pick track
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
                mode=mode if mode in ("buzzer", "race") else "buzzer",
                track=track,
                playlist_id=playlist_id,
                created_at=now_ms,
                start_at=start_at,
                buzz_close=buzz_close,
                phase="countdown",
            )
            new_round.add_event("round_started", {
                "mode": new_round.mode,
                "start_at": start_at,
                "buzz_close": buzz_close,
            })

            game["round"] = new_round
            game["last_active"] = now_ms

            return {"outcome": "round_started", "snapshot": self._snapshot_unlocked(game, host, now_ms)}

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
                "source": {"type": "audio", "id": str(round_state.track.id)},
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

            if round_state.phase == "revealed":
                winner_name = None
                if round_state.winner_pid and round_state.winner_pid in game["players"]:
                    winner_name = game["players"][round_state.winner_pid].name
                round_data["reveal"] = {
                    "title": round_state.track.title,
                    "winner": winner_name,
                }

        return {
            "server_time": now_ms,
            "code": game["code"],
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
