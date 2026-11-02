"""Local HTTP queue between the MCP tools and the Lightroom plug-in.

The plug-in polls ``GET /poll`` and answers with ``POST /result?id=N``. Everything is local and authenticated:
binds 127.0.0.1 only, random token, Host must match, requests with an Origin header (browsers) are refused.
"""
from __future__ import annotations

import hmac
import itertools
import json
import os
import queue
import secrets
import socket
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterable

from . import config


class BridgeBusy(RuntimeError):
    """Every port in the range is already used."""


class PluginNotConnected(RuntimeError):
    """The Lightroom plug-in has not polled recently."""


class BridgeError(RuntimeError):
    """The plug-in answered with an error, timed out or sent something unreadable."""


class BridgeTimeout(BridgeError):
    """The plug-in may have applied a write without returning its result."""


def restrict_to_current_user(path: Path) -> None:
    """Best effort: only the current user may read the token file (Windows ACL)."""
    user = os.environ.get("USERNAME")
    if os.name != "nt" or not user:
        return
    try:
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(M)"],
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="ascii")
    os.replace(tmp, path)


class _Server(ThreadingHTTPServer):
    """On Windows SO_REUSEADDR lets several processes bind the same port, which would break the
    one-instance rule (and let another program take the port). Bind exclusively instead."""

    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class Bridge:
    def __init__(
        self,
        port: int | None = None,
        data_dir: Path = config.DATA_DIR,
        poll_hold: float = config.POLL_HOLD_SECONDS,
        connect_wait: float = config.CONNECT_WAIT_SECONDS,
        ports: Iterable[int] | None = None,
        legacy_port: int | None = config.PORT,
    ):
        """``port`` (one fixed port) or ``ports`` (candidates, in order); default: the whole range. The server
        takes the first free one. ``legacy_port``: the server that gets this port also writes ~/LrMcp/token so that
        an old plug-in (0.2.x, which only knows port 58770) keeps working."""
        if ports is not None:
            self.candidates = list(ports)
        elif port is not None:
            self.candidates = [port]
        else:
            self.candidates = list(range(config.PORT, config.PORT + config.PORT_COUNT))
        self.port = self.candidates[0]
        self.legacy_port = legacy_port
        self.data_dir = data_dir
        self.servers_dir = data_dir / "servers"
        self.token_file = data_dir / "token"  # legacy token file
        self.info_file: Path | None = None
        self.poll_hold = poll_hold
        self.connect_wait = connect_wait
        self.token = secrets.token_urlsafe(32)
        self._jobs: "queue.Queue[tuple[int, str]]" = queue.Queue()
        self._events: dict[int, threading.Event] = {}
        self._results: dict[int, str] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._latest_gen = 0.0
        self.last_seen = 0.0
        self.plugin_version: str | None = None
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ---- lifecycle -------------------------------------------------
    def start(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        bridge = self

        class Handler(_Handler):
            owner = bridge

        self.servers_dir.mkdir(parents=True, exist_ok=True)
        self._remove_stale_info_files()
        for candidate in self.candidates:
            try:
                self._server = _Server((config.HOST, candidate), Handler)
            except OSError:
                continue
            self.port = candidate
            break
        else:
            first, last = self.candidates[0], self.candidates[-1]
            raise BridgeBusy(
                f"端口 {first}–{last} 都被占用了（最多同时运行 {len(self.candidates)} 个 Lightroom MCP）。"
                "请关闭不用的 AI 会话后再试。"
            )
        self._write_announcements()
        self._thread = threading.Thread(target=self._server.serve_forever, name="lrmcp-bridge", daemon=True)
        self._thread.start()

    # ---- announcing ourselves to the plug-in -----------------------
    def _write_announcements(self) -> None:
        """~/LrMcp/servers/<port>.txt tells the plug-in where to poll and with which token."""
        from . import __version__

        self.info_file = self.servers_dir / f"{self.port}.txt"
        _write_atomic(self.info_file, f"port={self.port}\ntoken={self.token}\npid={os.getpid()}\nversion={__version__}\n")
        restrict_to_current_user(self.info_file)
        if self.port == self.legacy_port:
            _write_atomic(self.token_file, self.token)
            restrict_to_current_user(self.token_file)

    def _remove_stale_info_files(self) -> None:
        """Delete announcements of servers that are gone (killed without cleaning up): nothing listens on their port."""
        for f in self.servers_dir.glob("*.txt"):
            try:
                port = int(f.stem)
            except ValueError:
                continue
            with socket.socket() as s:
                s.settimeout(0.3)
                alive = s.connect_ex((config.HOST, port)) == 0
            if not alive:
                f.unlink(missing_ok=True)

    def other_servers(self) -> list[int]:
        """Ports of the other running servers (other AI sessions)."""
        ports = []
        for f in self.servers_dir.glob("*.txt"):
            try:
                port = int(f.stem)
            except ValueError:
                continue
            if port != self.port:
                ports.append(port)
        return sorted(ports)

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self.info_file is not None:
            self.info_file.unlink(missing_ok=True)
        if self.port == self.legacy_port:
            self.token_file.unlink(missing_ok=True)

    # ---- state -----------------------------------------------------
    @property
    def connected(self) -> bool:
        return self.last_seen > 0 and time.time() - self.last_seen < config.PLUGIN_SEEN_SECONDS

    def wait_connected(self, timeout: float) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.connected:
                return True
            time.sleep(0.1)
        return self.connected

    # ---- calling the plug-in ---------------------------------------
    def call(self, command: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        """Send a whitelisted command to the plug-in and return its decoded JSON ``data``."""
        if not self.connected and not self.wait_connected(self.connect_wait):
            raise PluginNotConnected(
                "Lightroom 插件没有连上。请确认 Lightroom 已打开、已安装 LrMCP 插件；运行 lr_doctor 可查看原因。"
            )
        parts = [command]
        for key, value in (params or {}).items():
            text = str(value)
            if not key.isidentifier() or any(c.isspace() for c in text):
                raise ValueError(f"bad parameter {key!r}")
            parts.append(f"{key}={text}")
        job_id = next(self._ids)
        event = threading.Event()
        self._events[job_id] = event
        self._jobs.put((job_id, "cmd " + str(job_id) + " " + " ".join(parts)))
        if not event.wait(timeout):
            self._events.pop(job_id, None)
            raise BridgeTimeout(f"插件没有在 {timeout:.0f} 秒内回应命令 {command}。Lightroom 可能正忙或已卡住。")
        raw = self._results.pop(job_id, "")
        try:
            reply = json.loads(raw)
        except json.JSONDecodeError as e:
            raise BridgeError(f"插件返回的内容无法解析：{raw[:200]!r}") from e
        if not reply.get("ok"):
            raise BridgeError(str(reply.get("error", "未知错误")))
        return reply.get("data", {})

    # ---- used by the HTTP handler ----------------------------------
    def _heal_token_file(self) -> None:
        """If something deleted our announcement (or the legacy token file) while we are running, write it back (the
        plug-in re-reads it after a reload; without it Lightroom could not reconnect until this server restarts)."""
        if self.info_file is not None and (not self.info_file.exists() or (self.port == self.legacy_port and not self.token_file.exists())):
            try:
                self._write_announcements()
            except OSError:
                pass

    def _next_job(self, gen: float) -> str:
        self._heal_token_file()
        with self._lock:
            self.last_seen = time.time()
            if gen < self._latest_gen:
                return "stop"  # an older plug-in instance (before a reload) must exit
            self._latest_gen = gen
        try:
            _, line = self._jobs.get(timeout=self.poll_hold)
        except queue.Empty:
            return "none"
        return line

    def _store_result(self, job_id: int, body: str) -> None:
        self._results[job_id] = body
        event = self._events.pop(job_id, None)
        if event:
            event.set()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    owner: Bridge

    def log_message(self, format, *args):  # noqa: A002 - signature fixed by the base class
        pass

    def _reply(self, status: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _authorized(self) -> bool:
        if self.headers.get("Host") != f"{config.HOST}:{self.owner.port}":
            self._reply(400, "bad host")
            return False
        if self.headers.get("Origin") is not None:
            self._reply(403, "origin not allowed")
            return False
        got = self.headers.get("Authorization", "").encode("utf-8")
        if not hmac.compare_digest(got, f"Bearer {self.owner.token}".encode("utf-8")):
            self._reply(401, "unauthorized")
            return False
        return True

    def do_GET(self):
        if not self._authorized():
            return
        url = urllib.parse.urlparse(self.path)
        if url.path == "/poll":
            try:
                gen = float(self.headers.get("X-Gen") or 0)
            except ValueError:
                gen = 0.0
            self.owner.plugin_version = self.headers.get("X-Plugin-Version") or self.owner.plugin_version
            return self._reply(200, self.owner._next_job(gen))
        return self._reply(404, "not found")

    def do_POST(self):
        if not self._authorized():
            return
        url = urllib.parse.urlparse(self.path)
        if url.path == "/result":
            try:
                job_id = int(urllib.parse.parse_qs(url.query).get("id", ["0"])[0])
            except ValueError:
                return self._reply(400, "bad id")
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
            self.owner._store_result(job_id, body)
            return self._reply(200, "ok")
        return self._reply(404, "not found")
