"""Exercise the configured tools and their live TUI inside a disposable sandbox."""

import errno
import fcntl
import json
import os
from pathlib import Path
import pty
import re
import select
import shutil
import struct
import subprocess
from tempfile import TemporaryDirectory
import termios
import time


REPO = Path(__file__).resolve().parent.parent
ANSI = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]")
SANDBOX = re.compile(rb"Starting local sandbox (pi-[0-9a-f]{32}) in")
PROMPT = "Call the verify_pi_tools tool now with an empty object. Report its result briefly."


def check(workspace):
    extension_dir = workspace / ".pi/extensions"
    extension_dir.mkdir(parents=True)
    shutil.copyfile(REPO / "tests/tool_probe.ts", extension_dir / "probe.ts")
    report = workspace / "result.json"
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 48, 160, 0, 0))
    screen = bytearray()
    try:
        launcher = subprocess.Popen(
            [REPO / "bin/pi-sandbox", "--offline", "--no-session", "--approve", PROMPT],
            cwd=workspace, stdin=slave, stdout=slave, stderr=slave,
            env={**os.environ, "TERM": "xterm-256color"},
        )
    finally:
        os.close(slave)

    try:
        deadline = time.monotonic() + 420
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(master, 65_536)
                except OSError as error:
                    if error.errno != errno.EIO:
                        raise
                    break
                if not chunk:
                    break
                screen.extend(chunk)
            if report.is_file():
                break
            if launcher.poll() is not None and not ready:
                break
    finally:
        if launcher.poll() is None:
            launcher.terminate()
        try:
            launcher.wait(timeout=90)
        finally:
            os.close(master)

    sandbox = SANDBOX.search(screen)
    if not sandbox:
        raise RuntimeError("Launcher did not report starting a sandbox")
    listing = subprocess.run(["sbx", "--no-input", "ls", "--quiet"], check=True, capture_output=True)
    if sandbox[1] in listing.stdout.splitlines():
        raise RuntimeError("The tool check sandbox was not removed")
    visible = ANSI.sub(b"", screen).decode(errors="replace")
    if not report.is_file():
        for phase, message in (
            ("credential registration", "Cannot register"),
            ("extension loading", "Failed to load extension"),
            ("starting Pi", "Error:"),
        ):
            if message in visible:
                raise RuntimeError(f"No tool check report after {phase}")
        raise RuntimeError("No tool check report from Pi")
    result = json.loads(report.read_text())
    if "failure" in result:
        raise RuntimeError(f"{result['failure']}: {result['reason']}")
    if not re.search(r"#\d+ Verify shared tasks during main work.*…", visible):
        raise RuntimeError("The main task did not appear as active in the terminal")
    if "● Agents" not in visible or not all(
        f"Synthetic {role} child" in visible for role in ("general-purpose", "Explore", "Plan")
    ):
        raise RuntimeError("Child progress did not appear in the terminal")
    if (workspace / ".pi/tasks").exists():
        raise RuntimeError("Task state was written to the workspace")
    print("Main and child progress appeared in the terminal. All three children shared tasks and received the guidance.")
    print("Steered a running child and collected all three results. The sandbox was removed.")
    print(f"Exa returned {result['url']}, and fetch_content read it via {result['renderer']}.")


if __name__ == "__main__":
    try:
        with TemporaryDirectory(prefix="pi-tools-test-", dir="/tmp") as directory:
            check(Path(directory))
    except (RuntimeError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error)) from None
