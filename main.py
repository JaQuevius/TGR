"""Soundbite - a small, dependency-free party game server.

Run with ``python main.py`` and open http://localhost:8000 in a browser.
"""

from __future__ import annotations

import json
import base64
import io
import re
import secrets
import threading
import time
import uuid
import wave
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse


PORT = 8000
CODE_PATTERN = re.compile(r"^\d{4}$")
ROUND_LENGTH_SECONDS = 12


def make_demo_clip(frequencies: tuple[int, ...]) -> str:
    """Create a tiny, dependency-free demo clip for the built-in library."""
    sample_rate = 8000
    frames = bytearray()
    for frequency in frequencies:
        for sample in range(sample_rate // 2):
            value = int(9000 * ((sample % max(1, sample_rate // frequency)) / max(1, sample_rate // frequency) - 0.5))
            frames.extend(value.to_bytes(2, "little", signed=True))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as clip:
        clip.setnchannels(1)
        clip.setsampwidth(2)
        clip.setframerate(sample_rate)
        clip.writeframes(frames)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:audio/wav;base64,{encoded}"


TRACK_LIBRARY = [
    {"id": "sunrise", "title": "Sunrise on Sourdough", "artist": "The Loafers", "audio": make_demo_clip((392, 523, 659))},
    {"id": "midnight", "title": "Midnight at the Bakery", "artist": "Crumb Club", "audio": make_demo_clip((330, 440, 554))},
    {"id": "jams", "title": "Jam on Toast", "artist": "Butter & Beats", "audio": make_demo_clip((262, 330, 392))},
]


class GameStore:
    """Thread-safe in-memory lobby and round state."""

    def __init__(self) -> None:
        self._games: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def create_game(self, name: str) -> tuple[str, str]:
        with self._lock:
            while True:
                code = f"{secrets.randbelow(10000):04d}"
                if code not in self._games:
                    break
            host_id = uuid.uuid4().hex
            self._games[code] = {
                "host_id": host_id,
                "players": {
                    host_id: {"id": host_id, "name": name, "score": 0}
                },
                "round": None,
            }
            return code, host_id

    def join_game(self, code: str, name: str) -> str:
        with self._lock:
            game = self._games.get(code)
            if game is None:
                raise ValueError("That lobby code does not exist.")
            if game["round"] is not None:
                raise ValueError("This game has already started.")
            player_id = uuid.uuid4().hex
            game["players"][player_id] = {
                "id": player_id,
                "name": name,
                "score": 0,
            }
            return player_id

    def snapshot(self, code: str) -> dict[str, Any]:
        with self._lock:
            game = self._games.get(code)
            if game is None:
                raise ValueError("That lobby no longer exists.")
            round_state = game["round"]
            if round_state:
                elapsed = time.monotonic() - round_state["started_at"]
                if elapsed > ROUND_LENGTH_SECONDS and round_state["status"] == "playing":
                    round_state["status"] = "finished"
            players = sorted(
                game["players"].values(),
                key=lambda player: (-player["score"], player["name"].lower()),
            )
            public_round = None
            if round_state:
                public_round = {
                    "status": round_state["status"],
                    "audio": round_state["audio"],
                    "duration": round_state["duration"],
                    "started_at": round_state["started_at"],
                    "elapsed": max(0, elapsed),
                    "answers": round_state["answers"],
                    "winner": round_state["winner"],
                }
            return {"players": players, "round": public_round}

    def start_round(
        self,
        code: str,
        player_id: str,
        track_id: str,
    ) -> None:
        with self._lock:
            game = self._get_game_for_player(code, player_id)
            if game["host_id"] != player_id:
                raise ValueError("Only the host can start a round.")
            track = next((item for item in TRACK_LIBRARY if item["id"] == track_id), None)
            if track is None:
                raise ValueError("Choose a song from the library.")
            game["round"] = {
                "answer": normalize(track["title"]),
                "title": track["title"],
                "audio": track["audio"],
                "duration": ROUND_LENGTH_SECONDS,
                "started_at": time.monotonic(),
                "status": "playing",
                "answers": [],
                "winner": None,
            }

    def submit_answer(self, code: str, player_id: str, answer: str) -> dict[str, Any]:
        with self._lock:
            game = self._get_game_for_player(code, player_id)
            round_state = game["round"]
            if not round_state or round_state["status"] != "playing":
                raise ValueError("There is no active round.")
            if time.monotonic() - round_state["started_at"] > ROUND_LENGTH_SECONDS:
                round_state["status"] = "finished"
                raise ValueError("Time is up.")
            if any(item["player_id"] == player_id for item in round_state["answers"]):
                raise ValueError("You already answered this round.")
            if normalize(answer) != round_state["answer"]:
                raise ValueError("Not quite - keep trying.")

            placement = len(round_state["answers"])
            points = max(40, 100 - placement * 20)
            game["players"][player_id]["score"] += points
            result = {
                "player_id": player_id,
                "name": game["players"][player_id]["name"],
                "points": points,
                "answer": answer.strip(),
            }
            round_state["answers"].append(result)
            if round_state["winner"] is None:
                round_state["winner"] = result
            return result

    def finish_round(self, code: str, player_id: str) -> None:
        with self._lock:
            game = self._get_game_for_player(code, player_id)
            if game["host_id"] != player_id:
                raise ValueError("Only the host can finish a round.")
            if game["round"]:
                game["round"]["status"] = "finished"

    def _get_game_for_player(self, code: str, player_id: str) -> dict[str, Any]:
        game = self._games.get(code)
        if game is None or player_id not in game["players"]:
            raise ValueError("Your player session is no longer valid.")
        return game


def normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


STORE = GameStore()


PAGE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Soundbite</title>
  <style>
    :root { --ink:#fff8e7; --muted:#b99a72; --bg:#2b160d; --panel:#422314;
      --line:#704327; --lime:#ffd447; --orange:#f28b32; --bread:#f7b955; }
    * { box-sizing:border-box; }
    body { margin:0; background:radial-gradient(circle at 80% 5%,#8d4a20 0,transparent 35%),var(--bg);
      color:var(--ink); font:16px/1.45 Inter,ui-sans-serif,system-ui,sans-serif; min-height:100vh; }
    main { width:min(1040px,calc(100% - 32px)); margin:auto; padding:38px 0 64px; }
    header { display:flex; justify-content:space-between; align-items:center; margin-bottom:70px; }
    .brand { font-weight:900; letter-spacing:-.06em; font-size:26px; }
    .brand span { color:var(--bread); } .pill { border:1px solid var(--line); padding:7px 12px;
      border-radius:99px; color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.12em; }
    .hero { max-width:700px; margin-bottom:44px; } h1 { font-size:clamp(48px,8vw,94px); letter-spacing:-.08em;
      line-height:.9; margin:0 0 25px; } h1 em { color:var(--lime); font-style:normal; }
    .hero p { color:var(--muted); font-size:19px; max-width:520px; }
    .grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:18px; }
    .card { background:rgba(27,26,23,.9); border:1px solid var(--line); border-radius:22px; padding:27px; }
    .card h2 { margin:0 0 6px; letter-spacing:-.04em; font-size:25px; } .card p { color:var(--muted); margin:0 0 23px; }
    label { color:var(--muted); display:block; font-size:12px; text-transform:uppercase; letter-spacing:.1em; margin:16px 0 7px; }
    input,select,button { font:inherit; border-radius:11px; } input,select { width:100%; padding:13px 14px; border:1px solid var(--line);
      background:#2b160d; color:var(--ink); outline:none; } input:focus,select:focus { border-color:var(--lime); }
    button { border:0; padding:13px 17px; font-weight:800; cursor:pointer; background:var(--lime); color:#15170f; }
    button.secondary { background:transparent; color:var(--ink); border:1px solid var(--line); } button:disabled { opacity:.45; cursor:not-allowed; }
    .wide { width:100%; margin-top:20px; } .error { color:var(--orange); min-height:23px; margin-top:12px; font-size:14px; }
    .hidden { display:none !important; } .topline { display:flex; justify-content:space-between; align-items:center; gap:20px; }
    .code { color:var(--bread); font:900 48px/1 monospace; letter-spacing:.08em; } .code small { font:12px sans-serif; color:var(--muted);
      display:block; letter-spacing:.1em; margin-bottom:8px; } .players { margin:25px 0; display:grid; gap:8px; }
    .player { padding:13px 15px; border:1px solid var(--line); border-radius:12px; display:flex; justify-content:space-between; }
    .score { color:var(--lime); font-weight:800; } .game-layout { display:grid; grid-template-columns:1.3fr .7fr; gap:18px; }
    .audio-box { text-align:center; padding:45px 20px; background:#141510; border-radius:16px; margin:20px 0; }
    .wave { font-size:45px; letter-spacing:8px; color:var(--lime); animation:pulse 1.2s ease-in-out infinite alternate; }
    @keyframes pulse { from { opacity:.35; transform:scaleY(.75); } to { opacity:1; transform:scaleY(1.05); } }
    .timer { color:var(--orange); font-weight:800; font-size:20px; } .answer-row { display:flex; gap:10px; }
    .answer-row input { flex:1; } .answer-row button { white-space:nowrap; } .feed { margin-top:18px; color:var(--muted); font-size:14px; }
    .feed b { color:var(--ink); } .status { color:var(--lime); min-height:25px; margin-top:14px; }
    @media(max-width:700px) { main { padding-top:22px; } header { margin-bottom:44px; } .grid,.game-layout { grid-template-columns:1fr; }
      .card { padding:21px; } .code { font-size:38px; } }
  </style>
</head>
<body>
<main>
  <header><div class="brand">sound<span>bite</span></div><div class="pill">music • speed • points</div></header>
  <section id="home">
    <div class="hero"><h1>Hear it.<br><em>Claim it.</em></h1><p>Play the first seconds of a song. Be the quickest to name it and climb the scoreboard.</p></div>
    <div class="grid">
      <form class="card" id="host-form"><h2>Host a lobby</h2><p>Start a room and invite your friends.</p>
        <label for="host-name">Your name</label><input id="host-name" maxlength="24" placeholder="DJ Nova" required>
        <button class="wide">Create lobby <span>→</span></button><div class="error" id="host-error"></div>
      </form>
      <form class="card" id="join-form"><h2>Join a lobby</h2><p>Enter the four-digit code from your host.</p>
        <label for="join-name">Your name</label><input id="join-name" maxlength="24" placeholder="Your name" required>
        <label for="join-code">Lobby code</label><input id="join-code" inputmode="numeric" pattern="\d{4}" maxlength="4" placeholder="0000" required>
        <button class="wide">Join game <span>→</span></button><div class="error" id="join-error"></div>
      </form>
    </div>
  </section>
  <section id="lobby" class="hidden">
    <div class="topline"><div><div class="code"><small>lobby code</small><span id="lobby-code"></span></div></div><button class="secondary" onclick="leaveGame()">Leave lobby</button></div>
    <div class="grid" style="margin-top:30px"><div class="card"><h2>Players <span id="player-count"></span></h2><p>Share the code, then get ready.</p><div class="players" id="players"></div></div>
      <div class="card" id="host-controls"><h2>Pick a slice</h2><p>Choose a track from the bread library.</p>
        <label for="track-select">Song library</label><select id="track-select"></select>
        <button class="wide" id="start-round">Serve soundbite <span>▶</span></button><div class="error" id="round-error"></div>
      </div>
      <div class="card hidden" id="waiting"><h2>You're in.</h2><p>Wait for the host to start the next soundbite.</p><div class="wave">♪ ♫ ♪</div></div>
    </div>
  </section>
  <section id="game" class="hidden">
    <div class="topline"><div><div class="pill">lobby <span id="game-code"></span></div></div><div class="timer" id="timer"></div></div>
    <div class="game-layout" style="margin-top:18px"><div class="card"><h2 id="round-heading">Name that sound</h2><div class="audio-box"><div class="wave">♪ ♫ ♪</div><audio id="audio" controls></audio></div>
      <form class="answer-row" id="answer-form"><input id="answer" placeholder="Type the song name..." autocomplete="off" required><button>Answer</button></form>
      <div class="status" id="answer-status"></div><div class="feed" id="answer-feed"></div></div>
      <div class="card"><h2>Scoreboard</h2><div class="players" id="game-players"></div></div>
    </div>
  </section>
</main>
<script>
let session = null, poller = null, lastRound = null;
const $ = id => document.getElementById(id);
function show(id) { ["home","lobby","game"].forEach(x => $(x).classList.toggle("hidden", x !== id)); }
async function api(path, options = {}) {
  const response = await fetch(path, {headers: {"Content-Type":"application/json"}, ...options});
  const data = await response.json(); if (!response.ok) throw new Error(data.error || "Something went wrong."); return data;
}
function setSession(code, playerId, host) { session = {code, playerId, host}; localStorage.setItem("soundbite-session", JSON.stringify(session)); }
function error(id, message) { $(id).textContent = message; }
async function enterLobby() { show("lobby"); $("lobby-code").textContent = session.code; $("game-code").textContent = session.code; $("host-controls").classList.toggle("hidden", !session.host); $("waiting").classList.toggle("hidden", session.host); if (session.host) await loadLibrary(); await refresh(); poller = setInterval(refresh, 900); }
async function loadLibrary() { try { const tracks = await api("/api/library"); $("track-select").innerHTML = tracks.map(track => `<option value="${track.id}">${escapeHtml(track.title)} — ${escapeHtml(track.artist)}</option>`).join(""); } catch(e) { error("round-error",e.message); } }
async function refresh() {
  if (!session) return;
  try { const state = await api(`/api/game/${session.code}`); renderPlayers(state.players, "players"); renderPlayers(state.players, "game-players");
    $("player-count").textContent = `(${state.players.length})`;
    if (state.round && state.round.status === "playing") { if (!lastRound || lastRound.started_at !== state.round.started_at) { lastRound = state.round; show("game"); $("audio").src = state.round.audio; $("audio").play().catch(()=>{}); } renderRound(state.round); }
    else if (state.round && state.round.status === "finished" && lastRound) renderRound(state.round);
  } catch (e) { error("join-error", e.message); }
}
function renderPlayers(players, target) { if (!$(target)) return; $(target).innerHTML = players.map((p,i) => `<div class="player"><span>${i===0 && p.score ? "🏆 " : ""}${escapeHtml(p.name)}</span><span class="score">${p.score} pts</span></div>`).join(""); }
function renderRound(round) {
  $("round-heading").textContent = round.title || "Name that sound";
  $("timer").textContent = round.status === "playing" ? `${Math.max(0, Math.ceil(round.duration-round.elapsed))}s left` : "Round over";
  $("answer").disabled = round.status !== "playing"; $("answer-form").querySelector("button").disabled = round.status !== "playing";
  $("answer-feed").innerHTML = round.answers.map(a => `<div>⚡ <b>${escapeHtml(a.name)}</b> got it (+${a.points})</div>`).join("");
  if (round.winner) $("answer-status").textContent = `${round.winner.name} was first!`;
}
function escapeHtml(value) { return value.replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"}[c])); }
$("host-form").onsubmit = async e => { e.preventDefault(); error("host-error",""); try { const d = await api("/api/host", {method:"POST",body:JSON.stringify({name:$("host-name").value})}); setSession(d.code,d.player_id,true); enterLobby(); } catch(e) { error("host-error",e.message); } };
$("join-form").onsubmit = async e => { e.preventDefault(); error("join-error",""); try { const d = await api("/api/join", {method:"POST",body:JSON.stringify({code:$("join-code").value,name:$("join-name").value})}); setSession(d.code,d.player_id,false); enterLobby(); } catch(e) { error("join-error",e.message); } };
$("start-round").onclick = async () => { error("round-error",""); try { await api(`/api/game/${session.code}/round`, {method:"POST",body:JSON.stringify({player_id:session.playerId,track_id:$("track-select").value})}); } catch(e) { error("round-error",e.message); } };
$("answer-form").onsubmit = async e => { e.preventDefault(); $("answer-status").textContent = ""; try { const d = await api(`/api/game/${session.code}/answer`, {method:"POST",body:JSON.stringify({player_id:session.playerId,answer:$("answer").value})}); $("answer-status").textContent = `Correct! +${d.points} points`; $("answer").value = ""; } catch(e) { $("answer-status").textContent = e.message; } };
function leaveGame() { clearInterval(poller); session=null; lastRound=null; localStorage.removeItem("soundbite-session"); show("home"); }
const saved = JSON.parse(localStorage.getItem("soundbite-session") || "null"); if (saved) { session=saved; enterLobby(); }
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            body = PAGE.encode()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/library":
            self._send_json(
                [
                    {"id": track["id"], "title": track["title"], "artist": track["artist"]}
                    for track in TRACK_LIBRARY
                ]
            )
            return
        if parsed.path.startswith("/api/game/"):
            try:
                code = parsed.path.rsplit("/", 1)[-1]
                self._send_json(STORE.snapshot(code))
            except ValueError as exc:
                self._send_error(str(exc))
            return
        self._send_error("Not found.", HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        try:
            payload = self._payload()
            path = urlparse(self.path).path
            if path == "/api/host":
                name = clean_name(payload.get("name"))
                code, player_id = STORE.create_game(name)
                self._send_json({"code": code, "player_id": player_id})
            elif path == "/api/join":
                code = str(payload.get("code", "")).strip()
                if not CODE_PATTERN.fullmatch(code):
                    raise ValueError("Enter a four-digit lobby code.")
                name = clean_name(payload.get("name"))
                self._send_json({"code": code, "player_id": STORE.join_game(code, name)})
            else:
                match = re.fullmatch(r"/api/game/(\d{4})/(round|answer|finish)", path)
                if not match:
                    self._send_error("Not found.", HTTPStatus.NOT_FOUND)
                    return
                code, action = match.groups()
                player_id = str(payload.get("player_id", ""))
                if action == "round":
                    STORE.start_round(code, player_id, str(payload.get("track_id", "")))
                    self._send_json({"ok": True})
                elif action == "answer":
                    self._send_json(STORE.submit_answer(code, player_id, str(payload.get("answer", ""))))
                else:
                    STORE.finish_round(code, player_id)
                    self._send_json({"ok": True})
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._send_error(str(exc))

    def _payload(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 12_000_000:
            raise ValueError("That audio clip is too large.")
        return json.loads(self.rfile.read(length) or b"{}")

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, message: str, status: HTTPStatus = HTTPStatus.BAD_REQUEST) -> None:
        self._send_json({"error": message}, status)


def clean_name(value: Any) -> str:
    name = str(value or "").strip()
    if not name:
        raise ValueError("Enter a name first.")
    return name[:24]


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Soundbite is running at http://localhost:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Soundbite.")
    finally:
        server.server_close()