"""Exercise private Pi and audit host and guest writes in real local sandboxes."""

import errno
import fcntl
from io import BytesIO
import json
import os
from pathlib import Path
import pty
import re
import runpy
import select
import signal
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event
import termios
import time
from uuid import uuid4

sys.dont_write_bytecode = True
from privacy_watch import HostLogWatch


REPO = Path(__file__).resolve().parent.parent
LAUNCHER = REPO / "bin/pi-sandbox"
MODEL_POLICY = json.loads((REPO / "config/privacy-models.json").read_text())
DEFAULT = MODEL_POLICY["default"]
APPROVED = {(route["model"], route["provider"]) for route in MODEL_POLICY["routes"]}
MODEL = DEFAULT.split("/", 1)[1]
MARKER = re.compile(r"PI_PRIVACY_TRACE_[a-f0-9]{32}")
SANDBOX = re.compile(rb"Starting local sandbox (pi-[0-9a-f]{32}) in")
ANSI = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*?(?:\x07|\x1b\\)")
CURSOR_LINE = re.compile(rb"\x1b\[[\d;]+[Hf]|\x1b\[\d*[ABEF]")
MODEL_ROW = re.compile(r"^ *(?:→ )?(?:✓ )?([\w.-]+)(?: \([^)]+\))? \[([\w.-]+)\](?: · default)? *$", re.M)
PRIVATE_FOOTER = re.compile(rb"Privacy mode(?! active for)")
ARTIFACT = "privacy-audit-artifact.txt"
VISIBLE_DONE = "AUDIT_PRIVATE_RUN_COMPLETE"


def require(condition, description):
    if not condition:
        raise RuntimeError(description)


def visible_screen(data):
    return ANSI.sub(b"", CURSOR_LINE.sub(b"\n", data)).replace(b"\r", b"\n")


def tool_reply(name, arguments):
    return {"tool_calls": [{
        "index": 0, "id": uuid4().hex, "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }]}


class ScriptedProvider:
    """Replace the relay's upstream connection while keeping Pi and the relay intact."""

    def __init__(self, interrupt, artifact_contents):
        self.interrupt = interrupt
        self.artifact_contents = artifact_contents
        self.parent_turns = 0
        self.child_turns = 0

    def __call__(self, *_args, **_kwargs):
        provider = self

        class Connection:
            def connect(self):
                pass

            def request(self, _method, _path, *, body, headers):
                self.response = provider.respond(json.loads(body), headers)

            def getresponse(self):
                return self.response

            def close(self):
                pass

        return Connection()

    def respond(self, payload, headers):
        try:
            require(payload.get("model") == MODEL, "unexpected approved route")
            require(payload.get("stream") is True, "Pi did not request streaming")
            require(headers.get("Authorization") == "Bearer synthetic-provider-key", "missing synthetic key")
            messages = payload["messages"]
            marker = MARKER.search(json.dumps(messages))
            require(marker, "request did not contain the synthetic conversation marker")
            tools = {tool["function"]["name"] for tool in payload.get("tools", ())}
            require(not {"web_search", "fetch_content"} & tools, "private request advertised web tools")
            child = any("<active_agent " in json.dumps(message) for message in messages if message["role"] == "system")
            results = [json.dumps(message) for message in messages if message["role"] == "tool"]
            previous = results[-1] if results else ""
            if child:
                self.child_turns += 1
                turn = self.child_turns
                delta = self.child(turn, marker[0], previous)
            else:
                self.parent_turns += 1
                turn = self.parent_turns
                delta = self.parent(turn, marker[0], previous)
            print(f"AUDIT_PROVIDER_{'CHILD' if child else 'PARENT'}_{turn}", file=sys.stderr, flush=True)
            if child and turn == 3 and self.interrupt:
                Event().wait(timeout=50)
            require("tool_calls" not in delta or delta["tool_calls"][0]["function"]["name"] in tools,
                    "scripted test tool is absent")
            return self.sse(delta)
        except (RuntimeError, KeyError, TypeError, IndexError) as error:
            # Emit only our fixed check names. Never include payloads or error bodies.
            reason = str(error) if type(error) is RuntimeError else "malformed fixture request"
            print(f"AUDIT_PROVIDER_FAILED {reason}", file=sys.stderr, flush=True)
            return self.sse({"content": "AUDIT_PROVIDER_FAILED"})

    def parent(self, turn, marker, previous):
        child = {
            "subagent_type": "general-purpose",
            "description": "Privacy audit child progress",
            "prompt": f"Read the shared task list for {marker} and update it.",
            "run_in_background": False,
        }
        if turn == 1:
            return tool_reply("TaskCreate", {"subject": f"Privacy audit {marker}", "description": "Parent task"})
        if turn == 2:
            require("Task #" in previous, "parent could not create a shared task")
            return tool_reply("Agent", child)
        if turn == 3:
            require("AUDIT_CHILD_COMPLETE" in previous and self.child_turns == 3,
                    "approved child did not finish its task")
            return tool_reply("TaskList", {})
        if turn == 4:
            require(f"Child saw shared task {marker}" in previous, "parent could not see the child's task update")
            return tool_reply("write", {"path": ARTIFACT, "content": self.artifact_contents})
        require(turn == 5 and ARTIFACT in previous, "unexpected turn or missing workspace edit result")
        return {"content": VISIBLE_DONE}

    @staticmethod
    def child(turn, marker, previous):
        if turn == 1:
            return tool_reply("TaskList", {})
        if turn == 2:
            require(f"Privacy audit {marker}" in previous, "child could not see the parent's task")
            return tool_reply("TaskCreate", {"subject": f"Child saw shared task {marker}", "description": "Child update"})
        require(turn == 3 and "Task #" in previous, "child could not update the private shared task list")
        return {"content": "AUDIT_CHILD_COMPLETE"}

    @staticmethod
    def sse(delta):
        envelope = {"id": "pi-privacy-audit", "object": "chat.completion.chunk", "model": MODEL, "created": 0}
        finish = "tool_calls" if "tool_calls" in delta else "stop"
        choices = (
            {"index": 0, "delta": {"role": "assistant", **delta}, "finish_reason": None},
            {"index": 0, "delta": {}, "finish_reason": finish},
        )
        frames = "".join(f"data: {json.dumps(dict(envelope, choices=[choice]))}\n\n" for choice in choices)
        response = BytesIO((frames + "data: [DONE]\n\n").encode())
        response.status = 200
        response.getheader = lambda _name, _default: "text/event-stream"
        return response


def run_instrumented_launcher(mode, cleanup_fd, artifact_contents):
    """Run the production launcher with a synthetic provider and a cleanup checkpoint."""
    sys.path.insert(0, str(REPO))
    from sandbox import privacy

    privacy.go_credential = lambda _root: "synthetic-provider-key"
    privacy.HTTPSConnection = ScriptedProvider(mode == "interrupt", artifact_contents)
    real_run = subprocess.run

    def run(command, *args, **kwargs):
        if command[1:4] == ["--no-input", "rm", "--force"]:
            print("AUDIT_BEFORE_REMOVE", file=sys.stderr, flush=True)
            if select.select([cleanup_fd], [], [], 45)[0]:
                os.read(cleanup_fd, 1)
        return real_run(command, *args, **kwargs)

    subprocess.run = run
    sys.argv = [str(LAUNCHER), "--privacy"]
    runpy.run_path(str(LAUNCHER), run_name="__main__")


class Run:
    def __init__(self, workspace, mode):
        self.workspace = workspace
        self.mode = mode
        self.marker = ("PI_PRIVACY_TRACE_" + uuid4().hex).encode()
        self.artifact_contents = f"Workspace edit {uuid4().hex}\n"
        state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
        self.host = HostLogWatch(state / "sandboxes/sandboxes/sandboxd", self.marker)
        self.screen = bytearray()
        self.guest_screen = bytearray()
        self.guest = None
        self.name = None
        self.master, slave = pty.openpty()
        cleanup_read, self.cleanup_write = os.pipe()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 52, 180, 0, 0))
        try:
            self.launcher = subprocess.Popen(
                [sys.executable, "-B", __file__, "--launcher", mode, str(cleanup_read), self.artifact_contents],
                cwd=workspace, stdin=slave, stdout=slave, stderr=slave,
                pass_fds=(cleanup_read,), env={**os.environ, "TERM": "xterm-256color"},
            )
        finally:
            os.close(slave)
            os.close(cleanup_read)

    def drain(self):
        readers = {self.master: self.screen}
        if self.guest is not None:
            readers[self.guest.stdout.fileno()] = self.guest_screen
        for fd in select.select(list(readers), [], [], 0.1)[0]:
            try:
                data = os.read(fd, 65_536)
            except OSError as error:
                if error.errno != errno.EIO:
                    raise
            else:
                readers[fd].extend(data)
        self.host.check()
        for text, reason in (
            (b"EACCES: permission denied", "guest permission error"),
            (b"Failed to load extension", "guest extension failed to load"),
            (b"Cannot start privacy mode", "launcher refused private mode"),
            (b"Guest file monitor failed", "guest file monitor failed"),
        ):
            if text in self.screen or text in self.guest_screen:
                raise RuntimeError(reason)
        error = re.search(rb"AUDIT_PROVIDER_FAILED ([^\r\n]+)", self.screen)
        if error:
            raise RuntimeError(error[1].decode(errors="replace"))
        writes = re.findall(rb"AUDIT_GUEST_WRITE ([^\r\n]+)", self.guest_screen)
        if writes:
            path = writes[0].replace(self.marker, b"[marker]").decode(errors="replace")
            raise RuntimeError(f"Unexpected guest file write at {path}")

    def until(self, condition, label, timeout=40):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.drain()
            if condition():
                return
            if self.launcher.poll() is not None:
                break
        terminal = visible_screen(self.screen)
        signals = {
            "terminal_bytes": len(self.screen),
            "terminal_control": ANSI.search(self.screen) is not None,
            "launcher_banner": b"Privacy mode active for" in terminal,
            "privacy_status": PRIVATE_FOOTER.search(terminal) is not None,
            "model_picker": b"Model Name:" in terminal,
            "first_inference": b"AUDIT_PROVIDER_PARENT_1" in self.screen,
            "guest_monitor": b"AUDIT_GUEST_READY" in self.guest_screen,
            "launcher_status": self.launcher.poll(),
        }
        raise RuntimeError(f"Private {self.mode} run did not reach {label} ({signals})")

    def seen(self, value):
        return value in visible_screen(self.screen)

    def send(self, value):
        os.write(self.master, value)

    def check(self):
        self.until(lambda: SANDBOX.search(self.screen), "sandbox creation", timeout=90)
        self.name = SANDBOX.search(self.screen)[1].decode()
        print("Private sandbox allocation started.", flush=True)
        self.until(lambda: PRIVATE_FOOTER.search(visible_screen(self.screen)), "private UI status", timeout=420)
        print("Private UI status verified.", flush=True)
        self.send(b"\x0c")  # Pi's model-picker shortcut.
        self.until(lambda: self.seen(b"Model Name:"), "the model picker")
        picker = visible_screen(self.screen).decode(errors="replace")
        rows = set(MODEL_ROW.findall(picker))
        require(rows == APPROVED and f"✓ {MODEL}" in picker, "model picker showed a different whitelist or default")
        self.send(b"\x1b")
        print("Private model picker and selected default verified.", flush=True)
        if self.mode == "normal":
            self.check_guest_sdk()
            print("Private guest SDK route checks passed.", flush=True)
        source = (REPO / "tests/privacy_watch.py").read_text()
        self.guest = subprocess.Popen(
            ["sbx", "--no-input", "exec", "--interactive", "--user", "root", "--workdir",
             str(self.workspace), "--", self.name, "python3", "-u", "-c", source],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env={**os.environ, "SBX_NO_TELEMETRY": "1"},
        )
        self.until(lambda: b"AUDIT_GUEST_READY" in self.guest_screen, "the guest file monitor")
        print("Guest file monitor verified a transient control write.", flush=True)
        self.host.start_marker_window()
        self.send(b"Run this synthetic privacy audit with marker " + self.marker + b".\r")
        if self.mode == "interrupt":
            self.until(lambda: self.seen(b"AUDIT_PROVIDER_CHILD_3"), "the active child inference request", timeout=180)
            self.launcher.send_signal(signal.SIGTERM)
            expected = 128 + signal.SIGTERM
        else:
            self.until(lambda: self.seen(VISIBLE_DONE.encode()), "the completion reply", timeout=180)
            require(self.seen(b"Privacy audit child progress") and PRIVATE_FOOTER.search(visible_screen(self.screen)),
                    "private UI did not show the child progress and persistent indicator")
            self.send(b"\x04")
            expected = 0
        self.until(lambda: self.seen(b"AUDIT_BEFORE_REMOVE"), "post-execution inspection")
        self.guest.stdin.write(b"finish\n")
        self.guest.stdin.flush()
        self.until(lambda: b"AUDIT_GUEST_DONE" in self.guest_screen, "the final guest file inspection")
        require(self.guest.wait(timeout=15) == 0, "guest file monitor failed")
        self.release_cleanup()
        self.until(lambda: self.launcher.poll() is not None, "sandbox removal", timeout=90)
        require(self.launcher.returncode == expected, "private launcher returned an unexpected status")
        self.host.check()
        listing = subprocess.run(
            ["sbx", "--no-input", "ls", "--quiet"], capture_output=True, timeout=30,
            env={**os.environ, "SBX_NO_TELEMETRY": "1"},
        )
        require(listing.returncode == 0 and self.name.encode() not in listing.stdout.splitlines(),
                "private sandbox removal could not be confirmed")
        self.host.check()
        if self.mode == "normal":
            artifact = (self.workspace / ARTIFACT).read_text()
            require(artifact == self.artifact_contents and self.marker not in artifact.encode(),
                    "requested workspace edit did not persist with its own contents")

    def check_guest_sdk(self):
        # The probe uses synthetic relay settings and makes no approved request.
        environment = {
            "PI_SANDBOX_PRIVACY": "1", "PI_OFFLINE": "1",
            "PI_PRIVACY_BASE_URL": "http://host.docker.internal:1/v1",
            "PI_PRIVACY_RELAY_TOKEN": "synthetic-probe-token",
            "PI_CODING_AGENT_DIR": "/opt/pi/private-agent", "TMPDIR": "/tmp",
            "PI_TASKS": "/tmp/probe-tasks.json", "PI_CODING_AGENT_SESSION_DIR": "/tmp/probe-sessions",
        }
        options = [argument for key, value in environment.items() for argument in ("--env", f"{key}={value}")]
        result = subprocess.run(
            ["sbx", "--no-input", "exec", "--interactive", "--user", "agent", "--workdir",
             str(self.workspace), *options, "--", self.name, "node", "--input-type=module", "-"],
            input=(REPO / "tests/privacy_probe.mjs").read_bytes(), capture_output=True, timeout=45,
            env={**os.environ, "SBX_NO_TELEMETRY": "1"},
        )
        require(result.returncode == 0 and b"Private guest SDK checks passed." in result.stdout,
                "private guest SDK checks failed")

    def release_cleanup(self):
        if self.cleanup_write is not None:
            os.close(self.cleanup_write)
            self.cleanup_write = None

    def close(self):
        self.release_cleanup()
        if self.launcher.poll() is None:
            self.launcher.terminate()
        try:
            self.launcher.wait(timeout=90)
        finally:
            if self.guest is not None and self.guest.poll() is None:
                self.guest.terminate()
                self.guest.wait(timeout=15)
            os.close(self.master)
            self.host.close()


def main():
    with TemporaryDirectory(prefix="pi-privacy-audit-", dir="/tmp") as directory:
        for mode in ("normal", "interrupt"):
            workspace = Path(directory) / mode
            workspace.mkdir()
            run = Run(workspace, mode)
            try:
                print(f"Auditing private {mode} run", flush=True)
                run.check()
            finally:
                run.close()
    print("Private normal and interrupted runs completed with live guest and host file audits.")


if __name__ == "__main__":
    try:
        if sys.argv[1:2] == ["--launcher"]:
            run_instrumented_launcher(sys.argv[2], int(sys.argv[3]), sys.argv[4])
        else:
            main()
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        sys.exit(str(error))
    except KeyboardInterrupt:
        sys.exit(130)
