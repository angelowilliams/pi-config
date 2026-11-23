"""Check the real launcher on a host with a working local Docker Sandboxes login."""

import json
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
from tempfile import TemporaryDirectory
import time


LAUNCHER = Path(__file__).resolve().parent.parent / "bin/pi-sandbox"
PROBE = """
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

export default function (pi) {
  pi.on("session_start", async (_event, ctx) => {
    try {
      const guidance = readFileSync("/home/agent/.pi/agent/AGENTS.md", "utf8").trim();
      assert(ctx.getSystemPrompt().includes(guidance), "Global guidance missing from Pi context");
      const marker = "/tmp/pi-launcher-parent-marker";
      if (process.env.PI_LAUNCHER_TEST_CHILD === "1") {
        assert.equal(readFileSync(marker, "utf8"), "parent was here");
        process.exit(0);
      }

      assert.equal(process.cwd(), readFileSync("expected-cwd", "utf8"));
      assert.equal(process.env.HOME, "/home/agent");
      for (const key of ["PI_LAUNCHER_TEST_SECRET", "OPENAI_API_KEY", "SSH_AUTH_SOCK",
                         "DBUS_SESSION_BUS_ADDRESS"]) {
        assert(!(key in process.env), `Host variable reached Pi: ${key}`);
      }
      for (const file of ["../sibling/fake-secret", "absolute-link", "relative-link"]) {
        assert.throws(() => readFileSync(file), `Host file outside workspace readable: ${file}`);
      }
      assert.equal(process.env.PI_CODING_AGENT_SESSION_DIR, "/home/agent/.pi/sessions",
                   "Sessions are not stored on the host");
      const runs = resolve(process.env.PI_CODING_AGENT_SESSION_DIR, "launcher-check");
      writeFileSync(runs, String((existsSync(runs) ? Number(readFileSync(runs, "utf8")) : 0) + 1));
      assert(!existsSync(marker), "Previous invocation left guest state");
      writeFileSync(marker, "parent was here");
      const child = spawnSync("pi", ["--offline", "--no-session", "--mode", "rpc", "-e", resolve("probe.ts")], {
        env: { ...process.env, PI_LAUNCHER_TEST_CHILD: "1" }, encoding: "utf8", timeout: 20000,
      });
      assert(child.status === 0, `Child Pi probe failed: ${child.stderr || child.error}`);
      const boot = readFileSync("/proc/sys/kernel/random/boot_id", "utf8").trim();
      writeFileSync("report.json", JSON.stringify({ boot, env: Object.keys(process.env).sort() }));
      if (existsSync("hold")) await new Promise(() => setInterval(() => {}, 1000));
      process.exit(0);
    } catch (error) {
      console.error(error);
      process.exit(1);
    }
  });
}
"""


def output(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=30).stdout


def check_run(workspace, hold):
    if hold:
        (workspace / "hold").touch()
    report = workspace / "report.json"
    report.unlink(missing_ok=True)
    log = workspace.parent / "launcher.log"
    test_env = {
        **os.environ,
        "PI_LAUNCHER_TEST_SECRET": "fake-secret",
        "OPENAI_API_KEY": "fake-provider-key",
        "SSH_AUTH_SOCK": "/tmp/absent-fake-agent.sock",
    }
    with log.open("w") as handle, subprocess.Popen(
        [LAUNCHER, "--no-auth", "--offline", "--no-session", "--mode", "rpc", "-e", str(workspace / "probe.ts")],
        cwd=workspace, env=test_env, stdin=subprocess.DEVNULL, stdout=handle, stderr=handle,
    ) as launcher:
        try:
            deadline = time.monotonic() + 480
            while not report.exists() and launcher.poll() is None and time.monotonic() < deadline:
                time.sleep(0.2)
            assert report.exists(), f"Pi did not write the probe report\n{log.read_text()}"
            name = re.search(r"Starting local sandbox (pi-[0-9a-f]+) in", log.read_text())[1]
            sessions = re.search(r"Sessions are kept in (.+)", log.read_text())[1]
            if hold:
                details = json.loads(output("sbx", "inspect", name, "--format", "json"))
                mounts = details.get("host_mounts")
                assert isinstance(mounts, list) and len(mounts) == 2, (
                    f"Expected the workspace and session store mounts, got keys {sorted(details)} and {mounts}"
                )
                rendered = json.dumps(mounts)
                assert str(workspace) in rendered and sessions in rendered, (
                    f"Expected mounts for {workspace} and {sessions}, got {mounts}"
                )
                assert not details.get("mcp_servers"), "Unexpected static MCP servers in the sandbox"
                print("Host bind mounts", mounts, flush=True)
                launcher.send_signal(signal.SIGTERM)
            expected = 128 + signal.SIGTERM if hold else 0
            assert launcher.wait(timeout=60) == expected, log.read_text()
            assert name not in output("sbx", "ls", "--quiet").splitlines(), f"Sandbox {name} still exists"
            result = json.loads(report.read_text())
            print(f"{name} boot ID {result['boot']}, removed", flush=True)
            return result, sessions
        finally:
            if launcher.poll() is None:
                launcher.terminate()


def main():
    with TemporaryDirectory(prefix="pi-launcher-test-", dir="/tmp") as scratch:
        workspace = Path(scratch) / "workspace with spaces"
        sibling = Path(scratch) / "sibling"
        workspace.mkdir()
        sibling.mkdir()
        fake_secret = sibling / "fake-secret"
        fake_secret.write_text("host-only test fixture")
        (workspace / "absolute-link").symlink_to(fake_secret)
        (workspace / "relative-link").symlink_to("../sibling/fake-secret")
        (workspace / "expected-cwd").write_text(str(workspace))
        (workspace / "probe.ts").write_text(PROBE)

        first = check_run(workspace, hold=False)
        second = check_run(workspace, hold=True)
        assert first[0]["boot"] != second[0]["boot"], "Top-level invocations reused a kernel boot"
        assert first[1] == second[1], "Each run used a different session store"
        assert (Path(first[1]) / "launcher-check").read_text() == "2", "Guest writes did not persist on the host"
        shutil.rmtree(first[1])
        print("Sessions kept in", first[1])
        print("Guest environment variable names", second[0]["env"])
        print("Workspace changes, child marker and guidance, isolation, and cleanup verified.")


if __name__ == "__main__":
    main()
