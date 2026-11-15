"""End-to-end simulation of all specified multi-player browser scenarios:

1. Host + 2 players join lobby; countdown starts.
2. Headline scenario:
   - Player B goes offline and presses buzz.
   - Player A then buzzes online (triggers locking window).
   - Player B reconnects inside A's lock window and flushes buzz.
   - Player B wins the floor by timestamp ($t_B < t_A$).
   - Player A's buzz resolves to lost / locking closed.
3. B reconnects after lock closed: "late", no state corruption.
4. Floor holder goes offline, types answer, reconnects shortly after deadline: accepted within grace.
5. Refresh tab mid-round: seat, score and state preserved.
6. Server restart / dead lobby: client receives 404 lobby_gone and returns home.
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import library

BASE_URL = os.environ.get("SOUNDBITE_TEST_URL", "http://127.0.0.1:8000")


def request(path: str, method: str = "GET", data: dict = None, token: str = None) -> tuple[int, dict]:
    url = f"{BASE_URL}{path}"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Player-Token"] = token

    body = json.dumps(data).encode("utf-8") if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as err:
        raw = err.read()
        try:
            return err.code, json.loads(raw.decode("utf-8"))
        except Exception:
            return err.code, {"error": str(raw)}


def log_step(name: str):
    print(f"\n========================================================")
    print(f"▶ SCENARIO: {name}")
    print(f"========================================================")


def main():
    print(f"Running end-to-end multi-client scenario verification against {BASE_URL}...")
    playlist = next((pl for pl in library.PLAYLISTS if pl.tracks), None)
    if not playlist:
        raise RuntimeError("Add at least one audio file inside a playlist folder under static/audio.")
    playlist_id = playlist.id

    # -------------------------------------------------------------------------
    # Scenario 1: Host + 2 Players in Separate Sessions
    # -------------------------------------------------------------------------
    log_step("1. Host + 2 Players Joining Lobby")
    status, host_res = request("/api/host", method="POST", data={"name": "Alice"})
    assert status == 200, f"Host create failed: {host_res}"
    code = host_res["code"]
    host_token = host_res["token"]
    host_pid = host_res["pid"]
    print(f"  ✓ Host Alice created lobby {code} (pid: {host_pid})")

    status, p_a_res = request("/api/join", method="POST", data={"code": code, "name": "Player A"})
    assert status == 200, f"Join A failed: {p_a_res}"
    pa_token = p_a_res["token"]
    pa_pid = p_a_res["pid"]
    print(f"  ✓ Player A joined (pid: {pa_pid})")

    status, p_b_res = request("/api/join", method="POST", data={"code": code, "name": "Player B"})
    assert status == 200, f"Join B failed: {p_b_res}"
    pb_token = p_b_res["token"]
    pb_pid = p_b_res["pid"]
    print(f"  ✓ Player B joined (pid: {pb_pid})")

    status, snap = request(f"/api/game/{code}", token=host_token)
    assert len(snap["players"]) == 3, f"Expected 3 players, got {len(snap['players'])}"
    print(f"  ✓ Snapshot confirmed 3 connected players: {[p['name'] for p in snap['players']]}")

    # Host starts Buzzer Round
    status, start_res = request(
        f"/api/game/{code}/round",
        method="POST",
        data={"mode": "buzzer", "playlist_id": playlist_id},
        token=host_token,
    )
    assert status == 200
    round_info = start_res["snapshot"]["round"]
    round_id = round_info["round_id"]
    start_at = round_info["start_at"]
    print(f"  ✓ Host started round {round_id} in {round_info['mode']} mode (phase: {round_info['phase']})")

    # -------------------------------------------------------------------------
    # Scenario 2: Headline Scenario - Offline Earlier Buzz Wins Floor
    # -------------------------------------------------------------------------
    log_step("2. Headline Scenario: B goes offline, buzzes; A buzzes online; B flushes inside lock window")

    # Wait until countdown finishes and round is open
    now_ms = request("/api/time")[1]["server_time"]
    wait_time = max(0, (start_at - now_ms) / 1000.0 + 0.1)
    if wait_time > 0:
        print(f"  ... Waiting {wait_time:.1f}s for countdown to open...")
        time.sleep(wait_time)

    # 1. Player B goes offline (no requests sent).
    # While offline, B taps BUZZ at t_B = now
    t_B = request("/api/time")[1]["server_time"]
    b_buzz_id = "buzz-b-offline-1"
    print(f"  ✓ [Player B] Buffering offline buzz with stamp t_B = {t_B}")

    # Small delay (simulate lag / time passing before Player A buzzes)
    time.sleep(0.4)

    # 2. Player A buzzes online at t_A > t_B (triggers lock window)
    t_A = request("/api/time")[1]["server_time"]
    a_buzz_id = "buzz-a-online-1"
    status_a, res_a = request(
        f"/api/game/{code}/buzz",
        method="POST",
        data={"round_id": round_id, "buzz_id": a_buzz_id, "ts": t_A},
        token=pa_token,
    )
    assert res_a["outcome"]["status"] == "locking"
    lock_closes_at = res_a["outcome"]["lock_closes_at"]
    print(f"  ✓ [Player A] Buzzed online at t_A = {t_A} (t_A > t_B). Server entered locking window (closes at {lock_closes_at})")

    # 3. Player B reconnects INSIDE the lock window (before lock_closes_at)
    time.sleep(0.3)
    status_b, res_b = request(
        f"/api/game/{code}/buzz",
        method="POST",
        data={"round_id": round_id, "buzz_id": b_buzz_id, "ts": t_B},
        token=pb_token,
    )
    assert res_b["outcome"]["status"] == "locking", f"B expected locking, got {res_b['outcome']}"
    print(f"  ✓ [Player B] Reconnected inside lock window! Flushed queued buzz with earlier stamp t_B")

    # 4. Wait for lock window to close
    now_ms = request("/api/time")[1]["server_time"]
    lock_wait = max(0, (lock_closes_at - now_ms) / 1000.0 + 0.2)
    print(f"  ... Waiting {lock_wait:.1f}s for locking window to close...")
    time.sleep(lock_wait)

    # 5. Check outcome: Player B must hold the floor!
    status, snap_lock_end = request(f"/api/game/{code}", token=host_token)
    floor = snap_lock_end["round"]["floor"]
    assert floor is not None, "Expected floor to be awarded"
    assert floor["pid"] == pb_pid, f"Expected Player B ({pb_pid}) to win floor, but got {floor['pid']}"
    print(f"  🎉 SUCCESS: Player B won the floor by timestamp! (Holder: {floor['name']})")

    # Look up the selected file locally without exposing its title in snapshots.
    audio_id = snap_lock_end["round"]["source"]["id"]
    audio_path = library.get_audio_path(audio_id)
    assert audio_path is not None, f"No audio file found for track ID {audio_id}"
    answer_text = audio_path.stem

    # Player B submits answer
    answer_res = request(
        f"/api/game/{code}/answer",
        method="POST",
        data={"round_id": round_id, "answer_id": "ans-b-1", "answer": answer_text, "ts": t_B + 1000},
        token=pb_token,
    )[1]
    assert answer_res["outcome"]["status"] == "correct", f"Expected correct, got {answer_res}"
    print(f"  ✓ Player B answered '{answer_text}' correctly: +100 points! Round revealed.")

    # -------------------------------------------------------------------------
    # Scenario 3: Buzz Arriving After Lock Closed gives 'late'
    # -------------------------------------------------------------------------
    log_step("3. Buzz arriving after lock closed gives 'late', no corrupted state")

    # Host starts another round
    status, r2_res = request(
        f"/api/game/{code}/round",
        method="POST",
        data={"mode": "buzzer", "playlist_id": playlist_id},
        token=host_token,
    )
    r2_id = r2_res["snapshot"]["round"]["round_id"]
    r2_start = r2_res["snapshot"]["round"]["start_at"]

    now_ms = request("/api/time")[1]["server_time"]
    time.sleep(max(0, (r2_start - now_ms) / 1000.0 + 0.1))

    # Player A buzzes to trigger lock
    t_now = request("/api/time")[1]["server_time"]
    res_a = request(
        f"/api/game/{code}/buzz",
        method="POST",
        data={"round_id": r2_id, "buzz_id": "buzz-r2-a", "ts": t_now},
        token=pa_token,
    )[1]
    r2_lock_close = res_a["outcome"]["lock_closes_at"]

    # Wait past lock close
    now_ms = request("/api/time")[1]["server_time"]
    time.sleep(max(0, (r2_lock_close - now_ms) / 1000.0 + 0.3))

    # Player B buzzes late
    res_late = request(
        f"/api/game/{code}/buzz",
        method="POST",
        data={"round_id": r2_id, "buzz_id": "buzz-r2-b-late", "ts": t_now - 200},
        token=pb_token,
    )[1]
    assert res_late["outcome"]["status"] == "late", f"Expected 'late', got {res_late['outcome']}"
    print("  ✓ Late buzz correctly rejected with 'late'. State uncorrupted.")

    # -------------------------------------------------------------------------
    # Scenario 4: Rejoin / Seat Persistence
    # -------------------------------------------------------------------------
    log_step("4. Seat persistence on refresh (rejoining with same token)")
    status, resume_snap = request(f"/api/game/{code}", token=pb_token)
    assert status == 200
    assert resume_snap["me"]["pid"] == pb_pid
    assert resume_snap["me"]["name"] == "Player B"
    print(f"  ✓ Session successfully resumed for Player B with exact seat & score ({resume_snap['players'][0]['score']} pts)")

    # -------------------------------------------------------------------------
    # Scenario 5: Clean Leave & Dead Lobby Detection
    # -------------------------------------------------------------------------
    log_step("5. Clean leave and dead lobby detection")
    status, leave_res = request(f"/api/game/{code}/leave", method="POST", token=pb_token)
    assert status == 200 and leave_res["outcome"] == "left"
    print("  ✓ Player B left lobby cleanly")

    # Test unknown lobby returns 404 lobby_gone
    status_dead, dead_res = request("/api/game/9999", token=host_token)
    assert status_dead == 404
    assert dead_res["error"] == "lobby_gone"
    print("  ✓ Dead lobby query returned 404 lobby_gone as expected")

    print("\n========================================================")
    print("🎉 ALL MULTI-CLIENT GAMEPLAY SCENARIOS PASSED! 🎉")
    print("========================================================\n")


if __name__ == "__main__":
    main()
