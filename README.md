# 🍞 Soundbite: Server-Authoritative Buzzer Mode (Flaky Connection Resilient)

Soundbite is a real-time multiplayer music-guessing party game: a host creates a room with a 4-digit code, friends join on their phones, and each round plays a short soundbite.

This implementation features a **server-authoritative buzzer mode** built to survive flaky mobile Wi-Fi, packet drops, and subway dead zones using an offline resilience model:
> **A client that loses contact continues operating on its own** (NTP clock, countdown, audio, buzzing, typing), records what it did using server-synchronized timestamps, buffers actions into an outbox, and reconciles when it reconnects. The server decides outcomes by stamped time, not network arrival order, within a short grace window.

---

## 🚀 Quickstart

### 1. Requirements
- Python 3.9+ (Standard Library only — zero `pip` dependencies!)
- Modern web browser (Chrome, Safari, Firefox, Edge). Plain HTTP supported (no `crypto.randomUUID` requirement).

### 2. Run the Server
From the project folder:
```bash
python main.py
```
*(Optionally override the port: `python main.py --port 8080` or `PORT=8080 python main.py`).*

Use the URL printed by `main.py` for multiplayer. Do not open `static/index.html`
through a basic file server such as `python -m http.server`: that only serves
frontend files and has no `/api/host`, `/api/join`, or lobby polling endpoints,
so players cannot synchronize.

On startup, the server automatically detects and prints your LAN IP so phones can join directly:
```
🍞 ========================================================
🍞 Soundbite Party Game Server is running!
🍞 Local URL: http://localhost:8000
🍞 Phone LAN: http://192.168.1.150:8000
🍞 ========================================================
```

---

## 🎮 How to Play

1. Open `http://localhost:8000` (or the LAN URL on your phone).
2. **Host**: Click **Create lobby** and share the 4-digit code.
3. **Players**: Enter the 4-digit code and your name. (Joins are allowed at any time — late joiners can spectate the current round).
4. **Choose Mode**:
   - **Buzzer Mode**: Buzz in first to claim the floor. Correct answer earns **+100 points**; wrong answer deducts **-50 points** and eliminates you for the remainder of the round.
   - **Typing Race**: Everyone races to type the title. Points are awarded by placement (**100, 80, 60, 40**).
   - **My MP3s**: Local clips in `static/audio/` are streamed directly by the server.
   - **Popular YouTube hits**: The library includes recognizable songs such as *Shape of You*, *Despacito*, *Gangnam Style*, *Hello*, *Sugar*, and *Faded*.
5. **Multi-round Flow**: When the timer expires, the next song starts automatically. When the selected playlist is exhausted, the game returns everyone to the lobby. The host can also click **End Game** to return immediately.

---

## 🛠 How to Demo Offline Buzzer Resilience (`?debug=1`)

To test or demo the headline offline scenario:
1. Open two browser tabs or phones pointing to:
   ```
   http://localhost:8000/?debug=1
   ```
2. In Tab 1, create a lobby as **Host**. Select **Bread demo (offline tones)** and **Buzzer mode**.
3. In Tab 2, join as **Player B**.
4. In Tab 3, join as **Player A**.
5. Host clicks **Serve soundbite ▶**.

### The Headline Scenario:
1. In Player B's tab, expand the debug panel in the bottom right corner and check **"Simulate Disconnected"**.
2. Notice the top header badge immediately displays `✕ Offline`.
3. Player B presses the **BUZZ IN** button while offline:
   - The button shows `⏳ Buzzed, confirming...`.
   - The answer input opens immediately and Player B starts typing the answer (`Sunrise on Sourdough`).
4. Player A (still online) presses the **BUZZ IN** button:
   - The server enters the **locking window** (`LOCK_MS`).
5. Player B unchecks **"Simulate Disconnected"** to restore connection:
   - Player B's outbox automatically flushes the queued buzz to the server.
   - The server compares stamped timestamps ($t_B < t_A$): **Player B wins the floor retroactively!**
   - Player B's screen updates to `🎤 YOU HAVE THE FLOOR!` and automatically submits the early-typed answer.
   - Player A sees `🎤 Player B is answering...`.
   - Player B earns +100 points and the round is revealed!

---

## ⏱ Timing Constants & Tuning

All timing parameters are located at the top of [`game.py`](file:///c:/Users/richa/Durhack%202026/TGR/game.py) and can be overridden via environment variables:

| Environment Variable | Default | Purpose |
| :--- | :--- | :--- |
| `SOUNDBITE_COUNTDOWN_MS` | `3000` | Preload & countdown window before playback begins |
| `SOUNDBITE_ROUND_MS` | `12000` | Active buzz window for soundbite playback |
| `SOUNDBITE_LOCK_MS` | `1500` | Race window triggered on first received buzz |
| `SOUNDBITE_ANSWER_MS` | `8000` | Time floor holder has to submit their answer |
| `SOUNDBITE_RECONNECT_GRACE_MS` | `5000` | Extra grace period if floor holder drops offline |
| `SOUNDBITE_PRESENCE_MS` | `4000` | Time before a silent player is marked disconnected |
| `SOUNDBITE_CLOCK_SLACK_MS` | `300` | Tolerance window for honest clock drift |
| `SOUNDBITE_BACKDATE_SLACK_MS` | `750` | Maximum retroactive backdating allowed |

---

## 🧪 Running the Automated Tests

### 1. Python Unit Tests (Mock Clock)
Runs 14 unit and HTTP integration tests:
```bash
python -m unittest discover -s tests
```
Covers:
- Earlier-stamped buzzes arriving inside the lock window winning the floor.
- Late buzzes after lock closed marked `late` without corrupting state.
- Idempotent `buzz_id` and `answer_id` deduplication.
- Timestamp clamping and anti-cheat bounds.
- Wrong answers (-50 points, elimination, floor reopening).
- Answer timeouts for connected (-50) vs disconnected (0 penalty, grace).
- Nobody buzzing (straggler window).
- Secret leaks prevention (tokens never in snapshots, no answers before reveal).
- HTTP Range (206) audio streaming for iOS Safari.

### 2. Node.js Client Tests
Verifies DOM-free NTP clock synchronization and outbox logic:
```bash
node --test tests/test_client.js
```

### 3. Multi-Client End-to-End Simulation
Runs a complete simulated live match with concurrent clients against the server:
```bash
python tests/verify_scenarios.py
```

---

## 🎵 Audio Architecture

1. **In-Memory Synthesized Audio ("Bread demo")**:
   - Zero-dependency harmonic WAV synthesis generated at startup.
   - Ideal for offline testing and demos without external network needs.
2. **Audio Preloading into Blobs**:
   - Preloaded into a `Blob` URL during countdown so audio continues playing even if the connection drops mid-clip.
   - Direct HTTP 206 Partial Content (Range) support for iOS Safari.
3. **Hidden YouTube Player**:
   - YouTube IFrame API wrapper for curated playlists (90s, 2000s, 2010s, EDM).
   - Placed in a 200x200 container hidden behind an opaque bakery-themed visual curtain so video and title are never visible to players.

---

## 📁 Codebase Layout

- [`main.py`](file:///c:/Users/richa/Durhack%202026/TGR/main.py): HTTP server, explicit MIME router, Range streaming, and API dispatcher.
- [`game.py`](file:///c:/Users/richa/Durhack%202026/TGR/game.py): Pure game logic, GameStore, buzzer state machine, presence tracking, and snapshot filtering.
- [`library.py`](file:///c:/Users/richa/Durhack%202026/TGR/library.py): Track metadata, aliases, fuzzy normalization, and audio synthesis.
- [`static/index.html`](file:///c:/Users/richa/Durhack%202026/TGR/static/index.html): Semantic UI layout, tactile buzzer button, and debug panel.
- [`static/styles.css`](file:///c:/Users/richa/Durhack%202026/TGR/static/styles.css): Bakery design system, responsive buzzer states, and connection badges.
- [`static/clock.js`](file:///c:/Users/richa/Durhack%202026/TGR/static/clock.js): DOM-free NTP clock synchronization module.
- [`static/net.js`](file:///c:/Users/richa/Durhack%202026/TGR/static/net.js): DOM-free persistent outbox and connection manager.
- [`static/audio.js`](file:///c:/Users/richa/Durhack%202026/TGR/static/audio.js): Audio preloader, scheduler, and hidden YouTube controller.
- [`static/app.js`](file:///c:/Users/richa/Durhack%202026/TGR/static/app.js): Main client controller and state coordinator.
- [`tests/`](file:///c:/Users/richa/Durhack%202026/TGR/tests/): Test suite for python unit tests, HTTP tests, client tests, and scenario simulations.

The browser frontend lives entirely in [`static/`](file:///c:/Users/richa/Durhack%202026/TGR/static/).
Use that folder as the GitHub Pages publish directory; there is intentionally only
one frontend entry point, [`static/index.html`](file:///c:/Users/richa/Durhack%202026/TGR/static/index.html).

---

## 👥 Authors
- **Richard Okyne Ampah**
- **Aakanksh Prabhu**
- **Benjamin Woolsey**
- **Danny Jordan**

## 📄 License
This project is licensed under the MIT License - see the LICENSE file for details.
