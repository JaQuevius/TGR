"""Soundbite Server - Server-authoritative music party game.

Standard library Python only.
Serves static assets, handles audio streaming (including Range requests for iOS Safari),
and routes game state APIs.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

import game
import library

DEFAULT_PORT = 8000
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

# Explicit MIME mappings to avoid Windows registry quirks with .js
MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}

STORE = game.GameStore()


def get_lan_ip() -> str:
    """Best effort discovery of host LAN IP for mobile browser testing."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


class SoundbiteHandler(BaseHTTPRequestHandler):
    server_version = "Soundbite/2.0"

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress routine HTTP logs unless in verbose debugging
        if os.environ.get("SOUNDBITE_DEBUG"):
            super().log_message(format, *args)

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        # 1. API: Server epoch time
        if path == "/api/time":
            self._send_json({"server_time": STORE.now()})
            return

        # 2. API: Track library metadata (IDs and names only, NO answers)
        if path == "/api/library":
            self._send_json(library.get_playlists_catalog())
            return

        # 3. API: Game snapshot
        if path.startswith("/api/game/"):
            code = path[len("/api/game/") :].strip("/")
            if not game.CODE_PATTERN.fullmatch(code):
                self._send_error("Invalid lobby code format.", HTTPStatus.BAD_REQUEST)
                return

            token = self.headers.get("X-Player-Token", "").strip()
            if not token:
                self._send_error("Missing X-Player-Token header.", HTTPStatus.UNAUTHORIZED)
                return

            try:
                snapshot_data = STORE.snapshot(code, token)
                self._send_json(snapshot_data)
            except KeyError:
                self._send_error("lobby_gone", HTTPStatus.NOT_FOUND)
            except PermissionError:
                self._send_error("bad_token", HTTPStatus.UNAUTHORIZED)
            except Exception as exc:
                self._send_error(str(exc), HTTPStatus.BAD_REQUEST)
            return

        # 4. Audio Streaming Endpoint: /audio/<id>
        if path.startswith("/audio/"):
            audio_id = path[len("/audio/") :].strip("/")
            self._serve_audio(audio_id)
            return

        # 5. Static File Routing
        self._serve_static(path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        try:
            payload = self._parse_json_payload()

            # Host a new lobby
            if path == "/api/host":
                name = payload.get("name", "")
                code, pid, token = STORE.create_game(name)
                self._send_json({"code": code, "pid": pid, "token": token})
                return

            # Join an existing lobby
            if path == "/api/join":
                code = str(payload.get("code", "")).strip()
                if not game.CODE_PATTERN.fullmatch(code):
                    raise ValueError("Enter a valid four-digit lobby code.")
                name = payload.get("name", "")
                code, pid, token = STORE.join_game(code, name)
                self._send_json({"code": code, "pid": pid, "token": token})
                return

            # Match /api/game/<code>/<action>
            match = re.fullmatch(r"/api/game/(\d{4})/(round|buzz|answer|finish|leave)", path)
            if not match:
                self._send_error("Not found.", HTTPStatus.NOT_FOUND)
                return

            code, action = match.groups()
            token = self.headers.get("X-Player-Token", "").strip()
            if not token:
                self._send_error("Missing X-Player-Token header.", HTTPStatus.UNAUTHORIZED)
                return

            if action == "round":
                mode = str(payload.get("mode", "buzzer"))
                playlist_id = str(payload.get("playlist_id", ""))
                res = STORE.start_round(code, token, mode, playlist_id)
                self._send_json(res)
            elif action == "buzz":
                round_id = str(payload.get("round_id", ""))
                buzz_id = str(payload.get("buzz_id", ""))
                client_ts = int(payload.get("ts", STORE.now()))
                res = STORE.submit_buzz(code, token, round_id, buzz_id, client_ts)
                self._send_json(res)
            elif action == "answer":
                round_id = str(payload.get("round_id", ""))
                answer_id = str(payload.get("answer_id", ""))
                answer_text = str(payload.get("answer", ""))
                client_ts = int(payload.get("ts", STORE.now()))
                res = STORE.submit_answer(code, token, round_id, answer_id, answer_text, client_ts)
                self._send_json(res)
            elif action == "finish":
                res = STORE.finish_round(code, token)
                self._send_json(res)
            elif action == "leave":
                res = STORE.leave_game(code, token)
                self._send_json(res)

        except KeyError:
            self._send_error("lobby_gone", HTTPStatus.NOT_FOUND)
        except PermissionError:
            self._send_error("bad_token", HTTPStatus.UNAUTHORIZED)
        except (ValueError, TypeError) as exc:
            self._send_error(str(exc), HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self._send_error(str(exc), HTTPStatus.INTERNAL_SERVER_ERROR)

    # -------------------------------------------------------------------------
    # Static & Audio Serving
    # -------------------------------------------------------------------------

    def _serve_static(self, path: str) -> None:
        """Serves files from STATIC_DIR with path traversal defense and explicit MIME types."""
        rel_path = path.lstrip("/")
        if not rel_path or rel_path == "":
            rel_path = "index.html"

        # Safe path resolution
        full_path = os.path.abspath(os.path.join(STATIC_DIR, rel_path))
        if os.path.commonpath([STATIC_DIR, full_path]) != STATIC_DIR:
            self._send_error("Forbidden.", HTTPStatus.FORBIDDEN)
            return

        if not os.path.isfile(full_path):
            self._send_error("Not found.", HTTPStatus.NOT_FOUND)
            return

        ext = os.path.splitext(full_path)[1].lower()
        content_type = MIME_TYPES.get(ext, "application/octet-stream")

        try:
            with open(full_path, "rb") as f:
                body = f.read()

            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            # No-cache for client code to ensure fresh updates
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:
            self._send_error(f"Error reading file: {exc}", HTTPStatus.INTERNAL_SERVER_ERROR)

    def _serve_audio(self, audio_id: str) -> None:
        """Serves audio bytes supporting HTTP 206 Range requests for iOS Safari."""
        # 1. Check in-memory store first
        audio_bytes = library.get_audio_bytes(audio_id)
        content_type = "audio/wav"

        # 2. Check static/audio/<audio_id> on disk
        if not audio_bytes:
            for ext in (".wav", ".mp3", ".ogg"):
                candidate = os.path.join(STATIC_DIR, "audio", f"{audio_id}{ext}")
                if os.path.isfile(candidate):
                    with open(candidate, "rb") as f:
                        audio_bytes = f.read()
                    content_type = {
                        ".wav": "audio/wav",
                        ".mp3": "audio/mpeg",
                        ".ogg": "audio/ogg",
                    }[ext]
                    break

        if not audio_bytes:
            self._send_error("Audio clip not found.", HTTPStatus.NOT_FOUND)
            return

        total_len = len(audio_bytes)
        range_header = self.headers.get("Range")

        if range_header:
            match = re.match(r"bytes=(\d+)-(\d*)", range_header)
            if match:
                start = int(match.group(1))
                end = int(match.group(2)) if match.group(2) else total_len - 1
                if start < total_len and end < total_len and start <= end:
                    chunk = audio_bytes[start : end + 1]
                    self.send_response(HTTPStatus.PARTIAL_CONTENT)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Range", f"bytes {start}-{end}/{total_len}")
                    self.send_header("Content-Length", str(len(chunk)))
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Cache-Control", "public, max-age=31536000, immutable")
                    self.end_headers()
                    self.wfile.write(chunk)
                    return

        # Normal full 200 response
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(total_len))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.end_headers()
        self.wfile.write(audio_bytes)

    # -------------------------------------------------------------------------
    # Protocol Utilities
    # -------------------------------------------------------------------------

    def _parse_json_payload(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1_000_000:
            raise ValueError("Payload too large.")
        raw = self.rfile.read(length) if length > 0 else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    def _send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, message: str, status: HTTPStatus = HTTPStatus.BAD_REQUEST) -> None:
        self._send_json({"error": message}, status)


def run_server(port: int = DEFAULT_PORT) -> None:
    lan_ip = get_lan_ip()
    server = ThreadingHTTPServer(("0.0.0.0", port), SoundbiteHandler)

    print("🍞 ========================================================")
    print("🍞 Soundbite Party Game Server is running!")
    print(f"🍞 Local URL: http://localhost:{port}")
    print(f"🍞 Phone LAN: http://{lan_ip}:{port}")
    print("🍞 ========================================================")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Soundbite server.")
    finally:
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Soundbite Server")
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", str(DEFAULT_PORT))),
        help="Port to listen on (default 8000 or env PORT)",
    )
    args = parser.parse_args()
    run_server(args.port)
