"""Unit tests for Music Roulette: per-player upload folders and "guess who uploaded it" rounds.

Uses a temporary folder instead of static/audio/Music roulette so real uploads are never touched.
Run with: python -m unittest discover -s tests
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, ".")

import game
import library

MP3_BYTES = b"ID3fake-mp3-data"


class FakeClock:
    def __init__(self, start_ms: int = 1_000_000) -> None:
        self.current = float(start_ms)

    def __call__(self) -> float:
        return self.current

    def advance(self, ms: int) -> None:
        self.current += ms


class TestMusicRoulette(unittest.TestCase):
    def setUp(self) -> None:
        # Point Music Roulette at an empty temp folder for every test
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_dir = library.ROULETTE_DIR
        self._orig_tracks = list(library.ROULETTE_TRACKS)
        library.ROULETTE_DIR = Path(self._tmp.name) / library.ROULETTE_FOLDER_NAME
        library.ROULETTE_DIR.mkdir()
        library.ROULETTE_TRACKS.clear()

        self.clock = FakeClock()
        self.store = game.GameStore(clock_fn=self.clock)
        self.code, self.host_pid, self.host_token = self.store.create_game("Alice")
        _, self.bob_pid, self.bob_token = self.store.join_game(self.code, "Bob")
        _, self.cat_pid, self.cat_token = self.store.join_game(self.code, "Cat")

    def tearDown(self) -> None:
        for track in library.ROULETTE_TRACKS:
            library.AUDIO_STORE.pop(str(track.id), None)
        library.ROULETTE_TRACKS[:] = self._orig_tracks
        library.ROULETTE_DIR = self._orig_dir
        self._tmp.cleanup()

    def upload(self, token: str, filename: str = "song.mp3") -> dict:
        return self.store.upload_roulette_track(self.code, token, filename, MP3_BYTES, "audio/mpeg")

    def start_roulette(self) -> dict:
        res = self.store.start_round(self.code, self.host_token, "roulette", "")
        self.assertEqual(res["outcome"], "round_started")
        self.clock.advance(game.COUNTDOWN_MS)  # guessing opens when the song starts
        return res["snapshot"]["round"]

    def guess(self, token: str, round_id: str, guess_pid: str, answer_id: str) -> dict:
        return self.store.submit_answer(self.code, token, round_id, answer_id, guess_pid, int(self.clock()))

    # ------------------------------------------------------------------ uploads

    def test_upload_creates_player_folder_and_saves_song(self) -> None:
        res = self.upload(self.bob_token, "My Fave.mp3")
        saved = library.ROULETTE_DIR / "Bob" / "My Fave.mp3"
        self.assertTrue(saved.is_file())
        self.assertEqual(saved.read_bytes(), MP3_BYTES)
        self.assertEqual(res["my_count"], 1)

        # Same name again goes into the same folder without overwriting
        self.upload(self.bob_token, "My Fave.mp3")
        self.assertTrue((library.ROULETTE_DIR / "Bob" / "My Fave (2).mp3").is_file())
        self.assertEqual(len(list((library.ROULETTE_DIR / "Bob").iterdir())), 2)

        # The track's playlist says who uploaded it
        track = library.roulette_tracks_for(["Bob"])[0]
        self.assertEqual(library.roulette_owner_folder(track), "Bob")

    def test_upload_rejects_non_mp3_and_sanitises_names(self) -> None:
        with self.assertRaises(ValueError):
            self.store.upload_roulette_track(self.code, self.bob_token, "notes.txt", b"x", "text/plain")
        _, odd_pid, odd_token = self.store.join_game(self.code, "DJ/Cool:Name?")
        self.store.upload_roulette_track(self.code, odd_token, "../../evil.mp3", MP3_BYTES, "audio/mpeg")
        files = list(library.ROULETTE_DIR.rglob("*.mp3"))
        self.assertEqual(len(files), 1)
        # Stays inside the roulette folder, in a safe folder named after the player
        self.assertEqual(files[0].parent.parent, library.ROULETTE_DIR)
        self.assertEqual(files[0].parent.name, "DJ_Cool_Name_")

    def test_songs_from_earlier_sessions_are_loaded_from_disk(self) -> None:
        (library.ROULETTE_DIR / "Cat").mkdir()
        (library.ROULETTE_DIR / "Cat" / "old favourite.mp3").write_bytes(MP3_BYTES)
        library.load_roulette_tracks()
        tracks = library.roulette_tracks_for(["cat"])  # case-insensitive match
        self.assertEqual([t.title for t in tracks], ["old favourite"])

    def test_roulette_folder_is_not_a_normal_playlist(self) -> None:
        orig_audio_dir = library.AUDIO_DIR
        try:
            library.AUDIO_DIR = Path(self._tmp.name)
            (Path(self._tmp.name) / "Party").mkdir()
            (Path(self._tmp.name) / "Party" / "tune.mp3").write_bytes(MP3_BYTES)
            playlists, store = library.load_playlists()
            self.assertEqual([p.name for p in playlists], ["Party"])
            for key in store:
                library.AUDIO_STORE.pop(key, None)
        finally:
            library.AUDIO_DIR = orig_audio_dir

    # ------------------------------------------------------------------ rounds

    def test_round_needs_songs_and_two_players(self) -> None:
        with self.assertRaises(ValueError):
            self.store.start_round(self.code, self.host_token, "roulette", "")
        solo_code, _, solo_token = self.store.create_game("Solo")
        self.store.upload_roulette_track(solo_code, solo_token, "a.mp3", MP3_BYTES, "audio/mpeg")
        with self.assertRaises(ValueError):
            self.store.start_round(solo_code, solo_token, "roulette", "")

    def test_only_songs_from_players_in_this_lobby_are_played(self) -> None:
        (library.ROULETTE_DIR / "Stranger").mkdir()
        (library.ROULETTE_DIR / "Stranger" / "not here.mp3").write_bytes(MP3_BYTES)
        library.load_roulette_tracks()
        self.upload(self.bob_token, "bobs song.mp3")
        for _ in range(5):
            res = self.store.start_round(self.code, self.host_token, "roulette", "")
            if res["outcome"] == "game_finished":
                break
            current = self.store._games[self.code]["round"]
            self.assertEqual(current.owner_pid, self.bob_pid)
            self.assertEqual(current.track.title, "bobs song")

    def test_guessing_scoring_and_reveal(self) -> None:
        self.upload(self.bob_token, "secret.mp3")
        rnd = self.start_roulette()
        round_id = rnd["round_id"]

        # Nobody but Bob can tell it's Bob's song before the reveal
        alice_view = self.store.snapshot(self.code, self.host_token)["round"]
        self.assertFalse(alice_view["roulette"]["you_uploaded"])
        self.assertNotIn("reveal", alice_view)
        self.assertNotIn("secret", json.dumps(alice_view))
        self.assertTrue(self.store.snapshot(self.code, self.bob_token)["round"]["roulette"]["you_uploaded"])

        # The uploader can't guess their own song
        res = self.guess(self.bob_token, round_id, self.bob_pid, "b1")
        self.assertEqual(res["outcome"]["status"], "rejected")

        # Wrong guess: no points; only one guess each
        res = self.guess(self.cat_token, round_id, self.host_pid, "c1")
        self.assertEqual(res["outcome"]["status"], "incorrect")
        self.assertEqual(res["outcome"]["points"], 0)
        res = self.guess(self.cat_token, round_id, self.bob_pid, "c2")
        self.assertEqual(res["outcome"]["status"], "rejected")

        # Right guess scores, and once every non-uploader has guessed the round reveals
        res = self.guess(self.host_token, round_id, self.bob_pid, "a1")
        self.assertEqual(res["outcome"], {"status": "correct", "points": 100, "roulette": True})
        snap = res["snapshot"]
        self.assertEqual(snap["round"]["phase"], "revealed")
        reveal = snap["round"]["reveal"]
        self.assertEqual(reveal["owner"], "Bob")
        self.assertEqual(reveal["title"], "secret")
        self.assertEqual(reveal["winner"], "Alice")
        self.assertEqual(
            {(g["name"], g["guess_name"], g["correct"]) for g in reveal["guesses"]},
            {("Cat", "Alice", False), ("Alice", "Bob", True)},
        )
        scores = {p["name"]: p["score"] for p in snap["players"]}
        self.assertEqual(scores, {"Alice": 100, "Bob": 0, "Cat": 0})

        # Duplicate answer id is idempotent (outbox retries)
        again = self.guess(self.host_token, round_id, self.bob_pid, "a1")
        self.assertEqual(again["outcome"]["status"], "correct")
        self.assertEqual({p["name"]: p["score"] for p in again["snapshot"]["players"]}["Alice"], 100)

    def test_second_correct_guess_scores_less_and_guess_before_start_is_rejected(self) -> None:
        self.upload(self.cat_token, "tune.mp3")
        res = self.store.start_round(self.code, self.host_token, "roulette", "")
        round_id = res["snapshot"]["round"]["round_id"]
        early = self.guess(self.bob_token, round_id, self.cat_pid, "early")
        self.assertEqual(early["outcome"]["status"], "rejected")

        self.clock.advance(game.COUNTDOWN_MS)
        self.assertEqual(self.guess(self.bob_token, round_id, self.cat_pid, "b1")["outcome"]["points"], 100)
        self.assertEqual(self.guess(self.host_token, round_id, self.cat_pid, "a1")["outcome"]["points"], 80)

    def test_all_songs_played_finishes_game(self) -> None:
        self.upload(self.bob_token, "one.mp3")
        self.start_roulette()
        res = self.store.start_round(self.code, self.host_token, "roulette", "")
        self.assertEqual(res["outcome"], "game_finished")


if __name__ == "__main__":
    unittest.main()
