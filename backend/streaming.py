"""Private, bounded loopback media streams. Originals are never saved here."""
from __future__ import annotations

import contextlib
import math
import mimetypes
import posixpath
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .afc_io import read as afc_read
from .afc_io import iter_read as afc_iter_read

READ_SIZE = 1024 * 1024
MAX_SESSIONS = 160


def byte_range(value: str | None, size: int) -> tuple[int, int, bool]:
    """Single HTTP range. Invalid and unsatisfiable ranges both return 416."""
    if not value:
        return 0, size - 1, False
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value.strip())
    if not match or not any(match.groups()) or size <= 0:
        raise ValueError("Invalid range")
    first, last = match.groups()
    if not first:
        suffix = int(last)
        if not suffix:
            raise ValueError("Invalid suffix")
        return max(0, size - suffix), size - 1, True
    start = int(first)
    end = min(int(last), size - 1) if last else size - 1
    if start >= size or end < start:
        raise ValueError("Unsatisfiable range")
    return start, end, True


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, owner):
        self.owner = owner
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(("127.0.0.1", 0), _Handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            with contextlib.suppress(OSError):
                request.sendall(b"HTTP/1.1 503 Busy\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        # Expected disconnects do not print private source paths or tokens.
        pass


class StreamingServer:
    def __init__(self, engine):
        self.engine = engine
        self.lock = threading.RLock()
        self.sessions = {}
        self.readers = threading.BoundedSemaphore(4)
        self.transcoders = threading.BoundedSemaphore(2)
        self.closed = False
        self.encoder = None
        self.encoder_lock = threading.Lock()
        self.http = _Server(self)
        self.port = self.http.server_address[1]
        threading.Thread(target=self.http.serve_forever, name="iphone-media", daemon=True).start()

    def register(self, item, compatible=False, start=0, local=None, budget=None, lifetime=1800, allow_empty=False,
                 defer_phone_stat=False):
        from .engine import UserError, remote_path, signature
        try:
            start = float(start)
        except (ValueError, TypeError):
            raise UserError("Choose a valid video playback time.")
        if not math.isfinite(start) or not 0 <= start <= 24 * 3600:
            raise UserError("Choose a valid video playback time.")
        if compatible and (item["kind"] != "video" or not shutil.which("ffmpeg")):
            raise UserError("Compatible video playback needs FFmpeg. Open the setup guide for its installation command, then reopen EyeCopy.")
        path = remote_path(item["path"], item["source"])
        if local:
            local = Path(local).resolve()
            metadata = local.stat()
            size = metadata.st_size
            source_signature = (size, metadata.st_mtime_ns)
        else:
            if not self.engine._available(item["deviceId"]):
                raise UserError("Reconnect and unlock your Device to stream this file.")
            if defer_phone_stat:
                # The renderer cannot supply this metadata: Engine resolves
                # the item from its authoritative catalogue. Preparing a native
                # drag must finish while the mouse is still held, so postpone
                # opening House Arrest until Explorer requests actual bytes.
                try:
                    size = int(item["size"])
                except (ValueError, TypeError, KeyError):
                    raise UserError("Refresh the catalogue before dragging this file.")
                if not 0 <= size <= 2**63 - 1 or not isinstance(item.get("modified"), str):
                    raise UserError("Refresh the catalogue before dragging this file.")
                source_signature = {"size": size, "modified": item["modified"]}
            else:
                with self.engine._afc(item["deviceId"], item["source"], item.get("appId")) as afc:
                    self.engine._check_directory(afc, posixpath.dirname(path), item["source"])
                    metadata = afc.stat(path)
                    if metadata.get("st_ifmt") != "S_IFREG":
                        raise UserError("Choose an ordinary file to stream.")
                    source_signature = signature(metadata)
                    size = source_signature["size"]
        if size < 0 or (size == 0 and not allow_empty):
            raise UserError("This file is empty.")
        key = secrets.token_urlsafe(32)
        session = {"item": item, "path": path, "local": local, "size": size, "signature": source_signature,
                   "cancel": threading.Event(), "resources": set(), "deadline": time.monotonic() + lifetime,
                   "budget": budget, "used": 0, "compatible": bool(compatible), "start": start,
                   "contentType": mimetypes.guess_type(item["name"])[0] or "application/octet-stream"}
        with self.lock:
            for token, old in list(self.sessions.items()):
                if old["deadline"] < time.monotonic() and not old["resources"]:
                    self._revoke(token)
            while len(self.sessions) >= MAX_SESSIONS:
                idle = next((token for token, old in self.sessions.items() if not old["resources"]), None)
                if idle is None:
                    raise UserError("Media streams are busy. Close a preview or finish a transfer and try again.")
                self._revoke(idle)
            if self.closed:
                raise UserError("The media service is restarting. Try again.")
            self.sessions[key] = session
        mode = "compatible" if compatible else "original"
        return {"url": f"http://127.0.0.1:{self.port}/{key}/{mode}", "kind": item["kind"], "name": item["name"],
                "size": size, "modified": item["modified"], "mode": mode, "downloaded": bool(local), "start": start,
                "contentType": "video/mp4" if compatible else session["contentType"],
                "codec": "H.264 / AAC" if compatible else "Original", "seekable": not compatible,
                "streaming": not bool(local), "source": "pc" if local else "iphone"}

    def video_encoder(self):
        # Listing encoders is insufficient: NVENC may be compiled in while no
        # compatible GPU or driver is installed. Probe an actual tiny encode.
        with self.encoder_lock:
            if self.encoder is None:
                command = [shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                           "color=size=640x360:rate=1", "-frames:v", "1", "-c:v", "h264_nvenc", "-f", "null", "-"]
                try:
                    result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                            timeout=4, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    self.encoder = "h264_nvenc" if result.returncode == 0 else "libx264"
                except Exception:
                    self.encoder = "libx264"
            return self.encoder

    def _revoke(self, token):
        session = self.sessions.pop(token, None)
        if not session:
            return False
        session["cancel"].set()
        for resource in list(session["resources"]):
            with contextlib.suppress(Exception):
                if isinstance(resource, subprocess.Popen):
                    resource.kill()
                else:
                    resource.shutdown(socket.SHUT_RDWR)
                    resource.close()
        return True

    def revoke(self, url):
        try:
            parsed = urlsplit(str(url))
            valid = parsed.scheme == "http" and parsed.hostname == "127.0.0.1" and parsed.port == self.port
        except ValueError:
            valid = False
        if not valid:
            return {"stopped": False}
        with self.lock:
            return {"stopped": self._revoke(parsed.path.strip("/").split("/")[0])}

    def attach_process(self, url, process):
        """Allow engine shutdown to cancel bounded thumbnail decoders too."""
        token = urlsplit(url).path.strip("/").split("/")[0]
        with self.lock:
            session = self.sessions.get(token)
            if not session or session["cancel"].is_set():
                with contextlib.suppress(OSError):
                    process.kill()
                raise ConnectionAbortedError()
            self.resource(session, process)
            return session

    def close(self):
        with self.lock:
            self.closed = True
            for token in list(self.sessions):
                self._revoke(token)
        self.http.shutdown()
        self.http.server_close()

    def resource(self, session, value, add=True):
        with self.lock:
            if add:
                session["resources"].add(value)
                if session["cancel"].is_set():
                    if isinstance(value, subprocess.Popen):
                        value.kill()
                    else:
                        value.close()
                    raise ConnectionAbortedError()
            else:
                session["resources"].discard(value)

    def check(self, session):
        if session["cancel"].is_set() or session["deadline"] < time.monotonic() or self.engine.stop.is_set():
            raise ConnectionAbortedError()

    def allowance(self, session, requested):
        with self.lock:
            if session["budget"] is not None:
                requested = min(requested, session["budget"] - session["used"])
            if requested <= 0:
                raise ConnectionAbortedError("Preview read budget exhausted")
            session["used"] += requested
            return requested


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(12)

    def do_HEAD(self):
        self._serve(True)

    def do_GET(self):
        self._serve(False)

    def _error(self, status, message):
        body = message.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self.close_connection = True

    def _serve(self, head):
        owner = self.server.owner
        if self.headers.get("Host") != f"127.0.0.1:{owner.port}":
            self._error(403, "Invalid media host.")
            return
        parts = self.path.split("/")
        if len(parts) != 3 or parts[2] not in {"original", "compatible"}:
            self._error(404, "Unknown media stream.")
            return
        with owner.lock:
            session = owner.sessions.get(parts[1])
        if not session or session["cancel"].is_set() or session["deadline"] < time.monotonic():
            self._error(404, "Media stream expired. Open the preview again.")
            return
        if parts[2] == "compatible":
            if not session["compatible"]:
                self._error(404, "Unknown media stream.")
                return
            if head:
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                return
            self._compatible(owner, session, parts[1])
            return
        try:
            start, end, ranged = byte_range(self.headers.get("Range"), session["size"])
        except ValueError:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{session['size']}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if head:
            self._headers(session, start, end, ranged)
            return
        if not owner.readers.acquire(blocking=False):
            self._error(503, "Media readers are busy. Try again.")
            return
        try:
            owner.resource(session, self.connection)
            if session["local"]:
                metadata = session["local"].stat()
                if (metadata.st_size, metadata.st_mtime_ns) != session["signature"]:
                    self._error(409, "The local video changed. Open the preview again.")
                    return
                with session["local"].open("rb") as stream:
                    stream.seek(start)
                    self._headers(session, start, end, ranged)
                    remaining = end - start + 1
                    while remaining:
                        owner.check(session)
                        data = stream.read(owner.allowance(session, min(READ_SIZE, remaining)))
                        if not data:
                            raise ConnectionAbortedError()
                        self.wfile.write(data)
                        remaining -= len(data)
            else:
                self._phone(owner, session, start, end, ranged)
        except (BrokenPipeError, ConnectionError, OSError):
            self.close_connection = True
        finally:
            owner.resource(session, self.connection, False)
            owner.readers.release()

    def _headers(self, session, start, end, ranged):
        self.send_response(206 if ranged else 200)
        self.send_header("Content-Type", session["contentType"])
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if ranged:
            self.send_header("Content-Range", f"bytes {start}-{end}/{session['size']}")
        self.end_headers()

    def _phone(self, owner, session, start, end, ranged):
        from .engine import afc_seek, signature, public_error
        item = session["item"]
        headers_sent = False
        try:
            with owner.engine._afc(item["deviceId"], item["source"], item.get("appId")) as afc:
                sock = getattr(getattr(afc, "service", None), "socket", None)
                if sock:
                    owner.resource(session, sock)
                    sock.settimeout(3 if session["budget"] is not None else 12)
                try:
                    owner.check(session)
                    owner.engine._check_directory(afc, posixpath.dirname(session["path"]), item["source"])
                    metadata = afc.stat(session["path"])
                    if metadata.get("st_ifmt") != "S_IFREG" or signature(metadata) != session["signature"]:
                        self._error(409, "The Device file changed. Open the preview again.")
                        return
                    handle = afc.fopen(session["path"], "r")
                    try:
                        if start:
                            afc_seek(afc, handle, start)
                        self._headers(session, start, end, ranged)
                        headers_sent = True
                        remaining = end - start + 1
                        if session["budget"] is None:
                            with contextlib.closing(afc_iter_read(afc, handle, remaining,
                                                                 check=lambda: owner.check(session))) as chunks:
                                for data in chunks:
                                    self.wfile.write(data)
                        else:
                            while remaining:
                                owner.check(session)
                                requested = owner.allowance(session, min(READ_SIZE, remaining))
                                data = afc_read(afc, handle, requested)
                                if not data or len(data) > requested:
                                    raise ConnectionAbortedError()
                                self.wfile.write(data)
                                remaining -= len(data)
                    finally:
                        with contextlib.suppress(Exception):
                            afc.fclose(handle)
                finally:
                    if sock:
                        owner.resource(session, sock, False)
        except Exception as error:
            if not headers_sent:
                self._error(503, public_error(error))
            self.close_connection = True

    def _compatible(self, owner, session, token):
        if not owner.transcoders.acquire(blocking=False):
            self._error(503, "Compatible video players are busy. Close another preview first.")
            return
        process = None
        watchdog = None
        try:
            owner.check(session)
            owner.resource(session, self.connection)
            source = str(session["local"]) if session["local"] else f"http://127.0.0.1:{owner.port}/{token}/original"
            command = [shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin", "-threads", "2"]
            if not session["local"]:
                command += ["-rw_timeout", "12000000"]
            command += ["-probesize", "16000000", "-analyzeduration", "3000000", "-ss", str(session["start"]), "-i", source,
                        "-map", "0:v:0", "-map", "0:a:0?", "-vf", "scale=1280:720:force_original_aspect_ratio=decrease:force_divisible_by=2",
                        "-r", "30"]
            encoder = owner.video_encoder()
            if encoder == "h264_nvenc":
                command += ["-c:v", encoder, "-preset", "p1", "-tune", "ull", "-rc", "vbr", "-cq", "24", "-b:v", "5M"]
            else:
                command += ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-crf", "24"]
            command += [
                        "-threads", "2", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                        "-movflags", "+frag_keyframe+empty_moov+default_base_moof", "-frag_duration", "500000", "-f", "mp4", "pipe:1"]
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            owner.resource(session, process)
            last_output = [time.monotonic()]
            def idle_timeout():
                while process.poll() is None:
                    if session["cancel"].wait(1) or time.monotonic() - last_output[0] > 30:
                        with contextlib.suppress(OSError):
                            process.kill()
                        return
            watchdog = threading.Thread(target=idle_timeout, name="video-idle-timeout", daemon=True)
            watchdog.start()
            first = process.stdout.read(64 * 1024)
            if not first:
                self._error(415, "This video could not be decoded. Download the original and open it in your editor.")
                return
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            data = first
            while data:
                owner.check(session)
                last_output[0] = time.monotonic()
                self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                self.wfile.flush()
                data = process.stdout.read(64 * 1024)
            self.wfile.write(b"0\r\n\r\n")
        except Exception:
            self.close_connection = True
        finally:
            if process:
                with contextlib.suppress(OSError):
                    process.kill()
                with contextlib.suppress(Exception):
                    process.wait(timeout=3)
                if process.stdout:
                    process.stdout.close()
                owner.resource(session, process, False)
            owner.resource(session, self.connection, False)
            owner.transcoders.release()
