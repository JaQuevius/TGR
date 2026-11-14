"""HTTP integration test on an ephemeral port using urllib.

Tests hosting, joining, starting a round, audio range requests, and gameplay over HTTP.
Run with: python -m unittest discover -s tests
"""

import json
import threading
import time
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

import library
import main


class TestHttpIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # Bind to port 0 for an ephemeral OS-assigned port
        try:
            cls.server = ThreadingHTTPServer(("127.0.0.1", 0), main.SoundbiteHandler)
        except OSError as e:
            if getattr(e, "winerror", None) == 10013 or getattr(e, "errno", None) == 13:
                raise unittest.SkipTest("Socket creation restricted in current sandbox environment")
            raise

        cls.port = cls.server.server_address[1]
        cls.base_url = f"http://127.0.0.1:{cls.port}"

        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        time.sleep(0.05)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=1.0)

    def _request(self, path: str, method: str = "GET", data: dict = None, headers: dict = None) -> tuple[int, dict, dict]:
        url = f"{self.base_url}{path}"
        req_headers = {"Content-Type": "application/json"}
        if headers:
            req_headers.update(headers)

        body = json.dumps(data).encode("utf-8") if data else None
        req = urllib.request.Request(url, data=body, headers=req_headers, method=method)

        try:
            with urllib.request.urlopen(req) as resp:
                status = resp.status
                resp_headers = dict(resp.headers)
                raw = resp.read()
                try:
                    res_data = json.loads(raw.decode("utf-8"))
                except Exception:
                    res_data = raw
                return status, res_data, resp_headers
        except urllib.error.HTTPError as err:
            raw = err.read()
            try:
                err_data = json.loads(raw.decode("utf-8"))
            except Exception:
                err_data = raw
            return err.code, err_data, dict(err.headers)
        except urllib.error.URLError as err:
            if "[WinError 10013]" in str(err):
                raise unittest.SkipTest("Socket loopback connect restricted in current sandbox environment")
            raise

    def test_full_http_game_flow(self) -> None:
        # 1. Check /api/time
        status, time_res, _ = self._request("/api/time")
        self.assertEqual(status, 200)
        self.assertIn("server_time", time_res)

        # 2. Check /api/library (metadata only, no track titles leaked)
        status, lib_res, _ = self._request("/api/library")
        self.assertEqual(status, 200)
        self.assertIsInstance(lib_res, list)
        self.assertTrue(any(item["id"] == "bread-demo" for item in lib_res))

        # 3. Host creates lobby
        status, host_res, _ = self._request("/api/host", method="POST", data={"name": "Alice"})
        self.assertEqual(status, 200)
        code = host_res["code"]
        host_token = host_res["token"]
        self.assertTrue(bool(code))
        self.assertTrue(bool(host_token))

        # 4. Player joins lobby
        status, join_res, _ = self._request("/api/join", method="POST", data={"code": code, "name": "Bob"})
        self.assertEqual(status, 200)
        bob_token = join_res["token"]
        bob_pid = join_res["pid"]
        self.assertTrue(bool(bob_token))

        # 5. Snapshot via token header
        status, snap, _ = self._request(f"/api/game/{code}", headers={"X-Player-Token": host_token})
        self.assertEqual(status, 200)
        self.assertEqual(len(snap["players"]), 2)
        self.assertTrue(snap["me"]["host"])

        # 6. Audio Range Request: test HTTP 206 Partial Content
        req = urllib.request.Request(
            f"{self.base_url}/audio/sunrise",
            headers={"Range": "bytes=0-99"}
        )
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 206)
            self.assertEqual(resp.headers.get("Content-Range"), f"bytes 0-99/{len(library.get_audio_bytes('sunrise'))}")
            self.assertEqual(len(resp.read()), 100)

        # 7. Start Round
        status, round_res, _ = self._request(
            f"/api/game/{code}/round",
            method="POST",
            data={"mode": "buzzer", "playlist_id": "bread-demo"},
            headers={"X-Player-Token": host_token}
        )
        self.assertEqual(status, 200)
        round_info = round_res["snapshot"]["round"]
        self.assertEqual(round_info["phase"], "countdown")


if __name__ == "__main__":
    unittest.main()
