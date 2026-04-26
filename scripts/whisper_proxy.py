#!/usr/bin/env python3
"""Whisper proxy: converts any audio to WAV via ffmpeg, forwards to whisper-server.

whisper-server (whisper.cpp) only supports WAV input. WhatsApp sends voice messages
as OGG/Opus. This proxy sits on port 8002 (the port OpenClaw talks to), converts
incoming audio to 16kHz mono WAV via ffmpeg, and forwards the request to the
actual whisper-server on port 8003.
"""
import http.server
import subprocess
import tempfile
import os
import json
import urllib.request
import re
import sys

WHISPER_UPSTREAM = os.environ.get("WHISPER_UPSTREAM", "http://127.0.0.1:8003")
PORT = int(os.environ.get("WHISPER_PROXY_PORT", "8002"))


def parse_multipart(body, boundary):
    """Minimal multipart/form-data parser."""
    parts = {}
    sep = b"--" + boundary.encode() if isinstance(boundary, str) else b"--" + boundary
    chunks = body.split(sep)
    for chunk in chunks:
        if not chunk or chunk == b"--\r\n" or chunk.strip() == b"--":
            continue
        if b"\r\n\r\n" not in chunk:
            continue
        header_part, data = chunk.split(b"\r\n\r\n", 1)
        if data.endswith(b"\r\n"):
            data = data[:-2]
        headers_str = header_part.decode("utf-8", errors="replace")
        name_match = re.search(r'name="([^"]+)"', headers_str)
        filename_match = re.search(r'filename="([^"]+)"', headers_str)
        if not name_match:
            continue
        name = name_match.group(1)
        if filename_match:
            parts[name] = {"filename": filename_match.group(1), "data": data}
        else:
            parts[name] = data.decode("utf-8", errors="replace").strip()
    return parts


class TranscriptionHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            try:
                with urllib.request.urlopen(f"{WHISPER_UPSTREAM}/health", timeout=5):
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"status":"ok"}')
            except Exception:
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"upstream_down"}')
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if "/audio/transcriptions" not in self.path:
            self.send_response(404)
            self.end_headers()
            return

        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Expected multipart/form-data")
            return

        boundary_match = re.search(r'boundary=([^\s;]+)', content_type)
        if not boundary_match:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Missing boundary")
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        parts = parse_multipart(body, boundary_match.group(1))

        if "file" not in parts or not isinstance(parts["file"], dict):
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Missing file part")
            return

        file_info = parts["file"]
        audio_data = file_info["data"]
        filename = file_info["filename"] or "audio.ogg"
        model = parts.get("model", "whisper-1")
        language = parts.get("language", "")
        response_format = parts.get("response_format", "json")

        print(f"Received: {filename} ({len(audio_data)} bytes)", flush=True)

        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = os.path.join(tmpdir, filename)
            wav_path = os.path.join(tmpdir, "converted.wav")

            with open(input_path, "wb") as f:
                f.write(audio_data)

            needs_convert = not filename.lower().endswith(".wav")
            if needs_convert:
                ret = subprocess.run(
                    ["ffmpeg", "-y", "-i", input_path,
                     "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", wav_path],
                    capture_output=True, timeout=30
                )
                if ret.returncode != 0:
                    err = ret.stderr.decode()[:500]
                    print(f"ffmpeg failed: {err}", flush=True)
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": f"ffmpeg failed: {err}"}).encode())
                    return
                send_path = wav_path
                send_filename = "audio.wav"
            else:
                send_path = input_path
                send_filename = filename

            with open(send_path, "rb") as wf:
                wav_data = wf.read()
            print(f"Forwarding: {send_filename} ({len(wav_data)} bytes)", flush=True)

            boundary = b"----whisperproxy"
            body_parts = []
            body_parts.append(b"--" + boundary + b"\r\n")
            body_parts.append(
                f'Content-Disposition: form-data; name="file"; filename="{send_filename}"\r\n'.encode()
            )
            body_parts.append(b"Content-Type: audio/wav\r\n\r\n")
            body_parts.append(wav_data)
            body_parts.append(b"\r\n")
            body_parts.append(b"--" + boundary + b"\r\n")
            body_parts.append(
                f'Content-Disposition: form-data; name="model"\r\n\r\n{model}\r\n'.encode()
            )
            if language:
                body_parts.append(b"--" + boundary + b"\r\n")
                body_parts.append(
                    f'Content-Disposition: form-data; name="language"\r\n\r\n{language}\r\n'.encode()
                )
            if response_format:
                body_parts.append(b"--" + boundary + b"\r\n")
                body_parts.append(
                    f'Content-Disposition: form-data; name="response_format"\r\n\r\n{response_format}\r\n'.encode()
                )
            body_parts.append(b"--" + boundary + b"--\r\n")
            fwd_body = b"".join(body_parts)

            req = urllib.request.Request(
                f"{WHISPER_UPSTREAM}/v1/audio/transcriptions",
                data=fwd_body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary.decode()}"},
                method="POST"
            )
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    resp_data = resp.read()
                    print(f"Result: {resp.status} {resp_data[:200]}", flush=True)
                    self.send_response(resp.status)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(resp_data)
            except urllib.error.HTTPError as e:
                err_body = e.read()
                print(f"Upstream error: {e.code} {err_body[:200]}", flush=True)
                self.send_response(e.code)
                self.end_headers()
                self.wfile.write(err_body)
            except Exception as e:
                print(f"Upstream exception: {e}", flush=True)
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())

    def log_message(self, format, *args):
        pass  # suppress per-request access logs


if __name__ == "__main__":
    print(f"Whisper proxy on 127.0.0.1:{PORT} -> {WHISPER_UPSTREAM}", flush=True)
    server = http.server.HTTPServer(("127.0.0.1", PORT), TranscriptionHandler)
    server.serve_forever()
