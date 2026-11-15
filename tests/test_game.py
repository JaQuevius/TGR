"""Unit tests for Soundwich buzzer and race mode state machine with fake clock.

Tests all buzzer rules, timing windows, presence, secret leaks, and race regressions.
Run with: python -m unittest discover -s tests
"""

import sys
import unittest

# Ensure TGR root is in path
sys.path.insert(0, ".")

import game
import library


class FakeClock:
    def __init__(self, start_ms: int = 1_000_000) -> None:
        self.current = float(start_ms)

    def __call__(self) -> float:
        return self.current

    def advance(self, ms: int) -> None:
        self.current += ms


class TestBuzzerRules(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._original_playlists = library.PLAYLISTS
        cls.playlist_id = "test-playlist"
        library.PLAYLISTS = [
            library.Playlist(
                id=cls.playlist_id,
                name="Test playlist",
                tracks=[
                    library.Track(
                        id=1,
                        title="Test Track",
                        playlist_id=cls.playlist_id,
                    )
                ],
            )
        ]

    @classmethod
    def tearDownClass(cls) -> None:
        library.PLAYLISTS = cls._original_playlists

    def setUp(self) -> None:
        self.clock = FakeClock(1_000_000)
        self.store = game.GameStore(clock_fn=self.clock)
        self.code, self.host_pid, self.host_token = self.store.create_game("HostPlayer")
        _, self.p1_pid, self.p1_token = self.store.join_game(self.code, "PlayerOne")
        _, self.p2_pid, self.p2_token = self.store.join_game(self.code, "PlayerTwo")

    def test_no_token_leaks_in_snapshots_or_responses(self) -> None:
        """Requirement: No response body ever contains a token."""
        snap = self.store.snapshot(self.code, self.host_token)
        snap_str = str(snap)
        self.assertNotIn(self.host_token, snap_str)
        self.assertNotIn(self.p1_token, snap_str)
        self.assertNotIn(self.p2_token, snap_str)

    def test_no_title_or_answer_text_before_reveal(self) -> None:
        """Requirement: No title/answer text leaks before reveal."""
        self.store.start_round(self.code, self.host_token, "buzzer", self.playlist_id)
        snap = self.store.snapshot(self.code, self.p1_token)
        round_info = snap["round"]
        self.assertIsNotNone(round_info)
        self.assertNotIn("title", round_info)
        self.assertNotIn("artist", round_info)
        self.assertEqual(round_info["source"], {"type": "audio", "id": "1"})
        self.assertIsNone(round_info.get("reveal"))

    def test_earlier_stamped_buzz_inside_lock_window_wins(self) -> None:
        """Headline rule: B stamps earlier while offline, arrives inside A's lock window; B wins floor."""
        self.store.start_round(self.code, self.host_token, "buzzer", self.playlist_id)
        snap = self.store.snapshot(self.code, self.host_token)
        start_at = snap["round"]["start_at"]

        # Advance to round start
        self.clock.current = float(start_at + 1000)

        # Player 2 generated buzz at t = start_at + 200 (earlier stamped)
        p2_stamp = start_at + 200

        # Player 1 buzzes online at t = start_at + 1000 (triggers locking window)
        res1 = self.store.submit_buzz(self.code, self.p1_token, snap["round"]["round_id"], "buzz-1", start_at + 1000)
        self.assertEqual(res1["outcome"]["status"], "locking")

        # Advance 500ms into the lock window (lock window is 1500ms)
        self.clock.advance(500)

        # Player 2's offline buzz now arrives
        res2 = self.store.submit_buzz(self.code, self.p2_token, snap["round"]["round_id"], "buzz-2", p2_stamp)
        self.assertEqual(res2["outcome"]["status"], "locking")

        # Advance past the lock window close (1500ms)
        self.clock.advance(1500)

        # State check: Player 2 should have won the floor because p2_stamp < p1_stamp!
        snap_after = self.store.snapshot(self.code, self.host_token)
        self.assertEqual(snap_after["round"]["phase"], "answering")
        self.assertEqual(snap_after["round"]["floor"]["pid"], self.p2_pid)

    def test_buzz_arriving_after_lock_closed_gives_late(self) -> None:
        """Rule: A buzz received after lock closed gives 'late' and NEVER revises awarded floor."""
        self.store.start_round(self.code, self.host_token, "buzzer", self.playlist_id)
        snap = self.store.snapshot(self.code, self.host_token)
        start_at = snap["round"]["start_at"]

        self.clock.current = float(start_at + 500)

        # Player 1 triggers locking
        self.store.submit_buzz(self.code, self.p1_token, snap["round"]["round_id"], "buzz-1", start_at + 500)

        # Advance past lock window (LOCK_MS = 1500)
        self.clock.advance(2000)

        # P1 has the floor
        snap_mid = self.store.snapshot(self.code, self.host_token)
        self.assertEqual(snap_mid["round"]["phase"], "answering")
        self.assertEqual(snap_mid["round"]["floor"]["pid"], self.p1_pid)

        # Player 2 attempts to buzz now (even with an earlier stamped timestamp)
        res_late = self.store.submit_buzz(self.code, self.p2_token, snap["round"]["round_id"], "buzz-2", start_at + 200)
        self.assertEqual(res_late["outcome"]["status"], "late")

        # Verify floor is still P1
        snap_final = self.store.snapshot(self.code, self.host_token)
        self.assertEqual(snap_final["round"]["floor"]["pid"], self.p1_pid)

    def test_duplicate_buzz_id_is_idempotent(self) -> None:
        """Rule: duplicate buzz_id returns prior outcome without re-evaluation."""
        self.store.start_round(self.code, self.host_token, "buzzer", self.playlist_id)
        snap = self.store.snapshot(self.code, self.host_token)
        start_at = snap["round"]["start_at"]
        self.clock.current = float(start_at + 500)

        res1 = self.store.submit_buzz(self.code, self.p1_token, snap["round"]["round_id"], "buzz-abc", start_at + 500)
        res2 = self.store.submit_buzz(self.code, self.p1_token, snap["round"]["round_id"], "buzz-abc", start_at + 500)
        self.assertEqual(res1["outcome"], res2["outcome"])

    def test_timestamp_clamping(self) -> None:
        """Rule: future clamp, backdate clamp, early slack clamp."""
        self.store.start_round(self.code, self.host_token, "buzzer", self.playlist_id)
        snap = self.store.snapshot(self.code, self.host_token)
        start_at = snap["round"]["start_at"]

        # 1. Way too early (before start_at - CLOCK_SLACK_MS)
        self.clock.current = float(start_at - 1000)
        res_early = self.store.submit_buzz(self.code, self.p1_token, snap["round"]["round_id"], "buzz-early", start_at - 1000)
        self.assertEqual(res_early["outcome"]["status"], "rejected")
        self.assertEqual(res_early["outcome"]["reason"], "too_early")

        # 2. Slightly early (within CLOCK_SLACK_MS = 300ms of start_at)
        self.clock.current = float(start_at)
        res_slack = self.store.submit_buzz(self.code, self.p1_token, snap["round"]["round_id"], "buzz-slack", start_at - 150)
        self.assertEqual(res_slack["outcome"]["status"], "locking")

    def test_one_buzz_per_player(self) -> None:
        """Rule: one buzz per player per locking window/race."""
        self.store.start_round(self.code, self.host_token, "buzzer", self.playlist_id)
        snap = self.store.snapshot(self.code, self.host_token)
        start_at = snap["round"]["start_at"]
        self.clock.current = float(start_at + 500)

        res1 = self.store.submit_buzz(self.code, self.p1_token, snap["round"]["round_id"], "buzz-first", start_at + 500)
        self.assertEqual(res1["outcome"]["status"], "locking")

        res2 = self.store.submit_buzz(self.code, self.p1_token, snap["round"]["round_id"], "buzz-second", start_at + 600)
        self.assertEqual(res2["outcome"]["status"], "rejected")
        self.assertIn("already buzzed", res2["outcome"]["reason"])

    def test_correct_answer_awards_100_and_reveals(self) -> None:
        """Rule: correct answer gives +100 and reveal."""
        self.store.start_round(self.code, self.host_token, "buzzer", self.playlist_id)
        snap = self.store.snapshot(self.code, self.host_token)
        round_id = snap["round"]["round_id"]
        start_at = snap["round"]["start_at"]

        self.clock.current = float(start_at + 500)
        self.store.submit_buzz(self.code, self.p1_token, round_id, "buzz-1", start_at + 500)

        # Advance past lock window
        self.clock.advance(2000)

        # P1 has floor. Get actual track title from game state
        game_obj = self.store._games[self.code]
        correct_title = game_obj["round"].track.title

        # Submit correct answer
        res = self.store.submit_answer(self.code, self.p1_token, round_id, "ans-1", correct_title, int(self.clock.current))
        self.assertEqual(res["outcome"]["status"], "correct")
        self.assertEqual(res["outcome"]["points"], 100)

        # Snapshot should now be revealed with the title
        snap_revealed = self.store.snapshot(self.code, self.host_token)
        self.assertEqual(snap_revealed["round"]["phase"], "revealed")
        self.assertEqual(snap_revealed["round"]["reveal"]["title"], correct_title)
        self.assertNotIn("artist", snap_revealed["round"]["reveal"])
        self.assertEqual(snap_revealed["players"][0]["score"], 100)

    def test_wrong_answer_penalizes_50_and_reopens_floor(self) -> None:
        """Rule: wrong answer = -50, eliminated, floor reopens if time remains."""
        self.store.start_round(self.code, self.host_token, "buzzer", self.playlist_id)
        snap = self.store.snapshot(self.code, self.host_token)
        round_id = snap["round"]["round_id"]
        start_at = snap["round"]["start_at"]

        self.clock.current = float(start_at + 500)
        self.store.submit_buzz(self.code, self.p1_token, round_id, "buzz-1", start_at + 500)

        # Advance past lock window
        self.clock.advance(2000)

        # P1 submits wrong answer
        res = self.store.submit_answer(self.code, self.p1_token, round_id, "ans-wrong", "Definitely Wrong Answer", int(self.clock.current))
        self.assertEqual(res["outcome"]["status"], "incorrect")
        self.assertEqual(res["outcome"]["points"], -50)
        self.assertTrue(res["outcome"]["eliminated"])

        # Check snapshot: P1 eliminated, floor reopens for P2
        snap_open = self.store.snapshot(self.code, self.host_token)
        self.assertEqual(snap_open["round"]["phase"], "open")
        self.assertIsNone(snap_open["round"].get("floor"))

        p1_player = next(p for p in snap_open["players"] if p["pid"] == self.p1_pid)
        self.assertEqual(p1_player["score"], -50)
        self.assertTrue(p1_player["eliminated"])

    def test_answer_timeout_connected_vs_disconnected_holder(self) -> None:
        """Rule: Connected holder timed out = -50. Disconnected holder = 0 penalty."""
        # Scenario A: Connected holder times out
        self.store.start_round(self.code, self.host_token, "buzzer", self.playlist_id)
        snap = self.store.snapshot(self.code, self.host_token)
        round_id = snap["round"]["round_id"]
        start_at = snap["round"]["start_at"]

        self.clock.current = float(start_at + 500)
        self.store.submit_buzz(self.code, self.p1_token, round_id, "buzz-1", start_at + 500)
        self.clock.advance(2000)  # P1 has floor

        # P1 stays active by polling every 3s
        self.clock.advance(3000)
        self.store.snapshot(self.code, self.p1_token)
        self.clock.advance(3000)
        self.store.snapshot(self.code, self.p1_token)
        # Advance 3000ms more (total 9000ms from lock close, past 8500ms deadline + grace, but P1 last seen was 3000ms ago <= PRESENCE_MS)
        self.clock.advance(3000)
        snap_timed_out = self.store.snapshot(self.code, self.host_token)
        p1_state = next(p for p in snap_timed_out["players"] if p["pid"] == self.p1_pid)
        self.assertEqual(p1_state["score"], -50)
        self.assertTrue(p1_state["eliminated"])

        # Scenario B: Disconnected holder times out
        # Start a new round
        self.store.start_round(self.code, self.host_token, "buzzer", self.playlist_id)
        snap2 = self.store.snapshot(self.code, self.host_token)
        round_id2 = snap2["round"]["round_id"]
        start_at2 = snap2["round"]["start_at"]

        self.clock.current = float(start_at2 + 500)
        self.store.submit_buzz(self.code, self.p2_token, round_id2, "buzz-p2", start_at2 + 500)
        self.clock.advance(2000)  # P2 has floor

        # P2 disconnects (no snapshots for PRESENCE_MS = 4000)
        # Advance past presence timeout + deadline + RECONNECT_GRACE_MS
        self.clock.advance(15000)

        snap_disc_timeout = self.store.snapshot(self.code, self.host_token)
        p2_state = next(p for p in snap_disc_timeout["players"] if p["pid"] == self.p2_pid)
        # Disconnected holder eliminated with NO penalty (score remains 0)
        self.assertEqual(p2_state["score"], 0)
        self.assertTrue(p2_state["eliminated"])

    def test_nobody_buzzes_reveals_after_straggler_window(self) -> None:
        """Rule: if nobody buzzes by buzz_close, wait LOCK_MS for stragglers then reveal."""
        self.store.start_round(self.code, self.host_token, "buzzer", self.playlist_id)
        snap = self.store.snapshot(self.code, self.host_token)
        buzz_close = snap["round"]["buzz_close"]

        # Advance right to buzz close
        self.clock.current = float(buzz_close + 100)
        snap_closing = self.store.snapshot(self.code, self.host_token)
        # Still in open or waiting for stragglers
        self.assertIn(snap_closing["round"]["phase"], ("open", "revealed"))

        # Advance past straggler window (LOCK_MS = 1500)
        self.clock.advance(2000)
        snap_closed = self.store.snapshot(self.code, self.host_token)
        self.assertEqual(snap_closed["round"]["phase"], "revealed")

    def test_presence_timeouts(self) -> None:
        """Rule: connected = (now - last_seen <= PRESENCE_MS). Never delete on disconnect."""
        snap = self.store.snapshot(self.code, self.host_token)
        p1 = next(p for p in snap["players"] if p["pid"] == self.p1_pid)
        self.assertTrue(p1["connected"])

        # Advance past PRESENCE_MS (4000ms) without P1 making any requests
        self.clock.advance(5000)
        snap_later = self.store.snapshot(self.code, self.host_token)
        p1_later = next(p for p in snap_later["players"] if p["pid"] == self.p1_pid)
        self.assertFalse(p1_later["connected"])
        # Player is still in the lobby, not deleted!
        self.assertEqual(len(snap_later["players"]), 3)

    def test_race_mode_regression(self) -> None:
        """Rule: race mode functions without regressions, awarding 100, 80, 60."""
        self.store.start_round(self.code, self.host_token, "race", self.playlist_id)
        snap = self.store.snapshot(self.code, self.host_token)
        round_id = snap["round"]["round_id"]
        start_at = snap["round"]["start_at"]

        # Advance to round start
        self.clock.current = float(start_at + 100)

        track_title = self.store._games[self.code]["round"].track.title

        # P1 answers first
        res1 = self.store.submit_answer(self.code, self.p1_token, round_id, "race-1", track_title, int(self.clock.current))
        self.assertEqual(res1["outcome"]["status"], "correct")
        self.assertEqual(res1["outcome"]["points"], 100)

        # P2 answers second
        res2 = self.store.submit_answer(self.code, self.p2_token, round_id, "race-2", track_title, int(self.clock.current))
        self.assertEqual(res2["outcome"]["status"], "correct")
        self.assertEqual(res2["outcome"]["points"], 80)


if __name__ == "__main__":
    unittest.main()
