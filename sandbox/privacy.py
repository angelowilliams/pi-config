"""Host-only model validation and streaming for a private Pi sandbox."""

from contextlib import closing, contextmanager
import ctypes
from datetime import UTC, date, datetime
from hmac import compare_digest
from http.client import HTTPSConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import resource
import secrets
import subprocess
from threading import Event, Thread

from sandbox.auth import PI_VERSION, credential_directory, pi_command, pi_env, private_directory


PROVIDER = "opencode-go"
UPSTREAM_HOST = "opencode.ai"
UPSTREAM_PATH = "/inference/go/openai/v1/chat/completions"
CLIENT_PATH = "/v1/chat/completions"
MAX_BODY = 64 * 1024 * 1024
PR_SET_DUMPABLE = 4


def unique_object(entries):
    result = {}
    for name, value in entries:
        if name in result:
            raise ValueError("Duplicate JSON field")
        result[name] = value
    return result


def reject_constant(_):
    raise ValueError("Invalid JSON number")


def strict_json(data):
    return json.loads(data, object_pairs_hook=unique_object, parse_constant=reject_constant)


class Policy:
    def __init__(self, path):
        document = strict_json(path.read_text(encoding="utf-8"))
        self.default = document["default"]
        self.expiry = {}
        for route in document["routes"]:
            model = route["model"]
            valid_through = route.get("validThrough")
            if (route["provider"] != PROVIDER or not isinstance(model, str) or not model
                    or model in self.expiry or not isinstance(valid_through, str)):
                raise ValueError("Invalid privacy model route")
            try:
                self.expiry[model] = date.fromisoformat(valid_through)
            except ValueError:
                raise ValueError("Invalid privacy model route") from None
        prefix = f"{PROVIDER}/"
        if (not isinstance(self.default, str) or not self.default.startswith(prefix)
                or self.default.removeprefix(prefix) not in self.expiry):
            raise ValueError("The privacy default must be a whitelisted Go model")
        if not self.permits(self.default.removeprefix(prefix)):
            raise ValueError("The privacy default's retention agreement has expired")

    def permits(self, model):
        today = datetime.now(UTC).date()
        return isinstance(model, str) and model in self.expiry and today <= self.expiry[model]


def disable_core_dumps():
    # RLIMIT_CORE propagates to children. Dumpability also protects this process
    # when the host sends core dumps to a pipe instead of a regular file.
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    prctl = ctypes.CDLL(None, use_errno=True).prctl
    prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    if prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def go_credential(root):
    private_directory(root)
    agent = credential_directory(root)
    command = pi_command(root / f"pi-{PI_VERSION}")
    result = subprocess.run(
        [*command, "auth", "print-api-key", "--provider", PROVIDER],
        cwd=root, env=pi_env(root, agent), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20,
    )
    key = result.stdout.strip()
    if result.returncode or not key or any(byte < 33 or byte > 126 for byte in key):
        raise ValueError("OpenCode Go credentials unavailable. Run pi-sandbox-auth opencode-go")
    return key.decode("ascii")


class RelayHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass  # Never persist request paths, headers, bodies, or provider errors.

    def check_route(self, model):
        if self.server.stopping.is_set():
            self.send_error(503, "Privacy relay is closing")
            return False
        if not self.server.policy.permits(model):
            self.send_error(403, "Model is outside the active privacy whitelist")
            return False
        return True

    def do_POST(self):
        server = self.server
        self.connection.settimeout(60)
        if self.path != CLIENT_PATH:
            self.send_error(404, "Unknown privacy endpoint")
            return
        authorization = self.headers.get_all("Authorization", [])
        if len(authorization) != 1 or not compare_digest(authorization[0].encode("latin-1"), server.authorization):
            self.send_error(401, "Privacy relay authentication required")
            return
        lengths = self.headers.get_all("Content-Length", [])
        if (len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdecimal()
                or len(lengths[0]) > 9 or "Transfer-Encoding" in self.headers
                or self.headers.get_content_type() != "application/json"):
            self.send_error(400, "Invalid privacy request framing")
            return
        length = int(lengths[0])
        if not 0 < length <= MAX_BODY:
            self.send_error(413, "Privacy request exceeds the size limit")
            return
        try:
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError("Incomplete request body")
            payload = strict_json(body)
            if not isinstance(payload, dict):
                raise ValueError("Privacy request must be a JSON object")
            if "store" in payload and payload["store"] is not False:
                raise ValueError("Privacy requests cannot enable provider storage")
            canonical = json.dumps(payload, allow_nan=False).encode("ascii")
        except (ValueError, OSError, RecursionError):
            self.send_error(400, "Invalid privacy request")
            return
        model = payload.get("model")
        if not self.check_route(model):
            return

        sessions = self.headers.get_all("X-OpenCode-Session", [])
        if len(sessions) != 1 or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", sessions[0]):
            self.send_error(400, "Privacy requests need a valid OpenCode session header")
            return
        agent = self.headers.get("User-Agent")
        if not agent or not re.fullmatch(r"[ -~]{1,512}", agent):
            agent = f"Pi/{PI_VERSION} privacy-relay"
        headers = {
            "Authorization": f"Bearer {server.api_key}", "Content-Type": "application/json",
            "Accept": "text/event-stream, application/json",
            "X-OpenCode-Session": sessions[0], "User-Agent": agent,
        }
        with closing(HTTPSConnection(UPSTREAM_HOST, timeout=120)) as upstream:
            try:
                upstream.connect()
                # An accepted connection may finish negotiating after shutdown.
                if not self.check_route(model):
                    return
                upstream.request("POST", UPSTREAM_PATH, body=canonical, headers=headers)
                response = upstream.getresponse()
            except (OSError, ValueError, HTTPException):
                self.send_error(502, "Privacy provider connection failed")
                return
            if response.status != 200:
                status = response.status if 400 <= response.status <= 599 else 502
                self.send_error(status, "Privacy provider rejected the request")
                return
            self.send_response(200)
            media_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
            self.send_header("Content-Type", "text/event-stream" if media_type == "text/event-stream" else "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            while not server.stopping.is_set() and (chunk := response.read1(65536)):
                self.wfile.write(chunk)


class RelayServer(ThreadingHTTPServer):
    def handle_error(self, *_):
        pass  # Request exceptions may contain provider or user data.


@contextmanager
def model_relay(policy, api_key):
    token = secrets.token_urlsafe(32)
    with RelayServer(("127.0.0.1", 0), RelayHandler) as server:
        server.policy = policy
        server.api_key = api_key
        server.authorization = f"Bearer {token}".encode("ascii")
        server.stopping = Event()
        worker = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        worker.start()
        try:
            yield server.server_port, token
        finally:
            server.stopping.set()
            server.shutdown()
            worker.join()
