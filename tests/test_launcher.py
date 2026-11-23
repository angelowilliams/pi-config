from datetime import date
import importlib.util
from importlib.machinery import SourceFileLoader
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from sandbox.auth import PI_VERSION, save_exa_credential


REPO = Path(__file__).resolve().parent.parent
FAKE_SBX = """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import signal
import sys
import time

arguments = sys.argv[1:]
_, command, *options = arguments
record = {"args": arguments, "env": sorted(os.environ)}
if command == "create":
    record["staged"] = any(Path(option, "agent/AGENTS.md").is_file() for option in options)
if command == "exec" and not Path("hold").exists():
    record["stdin"] = sys.stdin.read()
with Path("sbx.jsonl").open("a") as log:
    log.write(json.dumps(record) + "\\n")
if command == "secret" and options[:1] == ["set-custom"]:
    print("routine registration notice", file=sys.stderr)
if command == "create" and Path("fail").exists():
    sys.exit(41)
if command == "create" and Path("hold-create").exists():
    time.sleep(0.5)
    Path("created").touch()
if command == "exec" and Path("hold").exists():
    signal.pause()
if command == "secret" and options == ["ls", "--global", "--quiet"] and Path("global-secret").exists():
    print("fake global credential name")
if command == "secret" and options[:1] == ["set-custom"] and options[options.index("--host") + 1] == "api.openai.com":
    if Path("fail-auth").exists():
        sys.exit(42)
if command == "secret" and options[:1] == ["set-custom"] and options[options.index("--host") + 1] == "api.exa.ai":
    if Path("fail-exa").exists():
        sys.exit(43)
"""


class LauncherTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(self.enterContext(TemporaryDirectory()))
        self.workspace = scratch / "project with spaces"
        self.workspace.mkdir()
        commands = scratch / "commands with spaces"
        commands.mkdir()
        (commands / "sbx").write_text(FAKE_SBX)
        (commands / "sbx").chmod(0o755)
        (commands / "pi").write_text("#!/bin/sh\ntouch host-pi-ran\n")
        (commands / "pi").chmod(0o755)
        self.home = scratch / "host home"
        self.home.mkdir()
        self.auth_root = self.home / ".local/share/pi-sandbox/auth-runtime"
        cli = self.auth_root / f"pi-{PI_VERSION}/node_modules/.bin/pi"
        cli.parent.mkdir(parents=True)
        cli.touch()
        agent = self.auth_root / "agent"
        agent.mkdir(mode=0o700)
        self.auth_root.chmod(0o700)
        (self.auth_root / f"pi-{PI_VERSION}").chmod(0o700)
        (agent / "auth.json").write_text("fake-credential-store")
        (agent / "auth.json").chmod(0o600)
        self.env = {
            "PATH": f"{commands}:{os.defpath}",
            "HOME": str(self.home),
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/test/bus",
            "PI_TEST_SECRET": "fake-secret",
            "OPENCODE_API_KEY": "fake-host-go",
            "OPENAI_API_KEY": "fake-host-openai",
            "EXA_API_KEY": "fake-host-exa",
            "SBX_CLOUD": "true",
        }
        self.launcher = REPO / "bin/pi-sandbox"

    def run_launcher(self, *arguments, **options):
        options.setdefault("cwd", self.workspace)
        return subprocess.run(
            [self.launcher, *arguments], env=self.env,
            capture_output=True, text=True, timeout=20, **options,
        )

    def creates(self):
        return [entry["args"] for entry in self.trace() if entry["args"][1] == "create"]

    def trace(self):
        return [json.loads(line) for line in (self.workspace / "sbx.jsonl").read_text().splitlines()]

    def test_passes_only_workspace_and_pi_arguments(self):
        result = self.run_launcher("--model", "test model", "a prompt", input="input from a pipe")
        self.assertEqual(result.returncode, 0, result.stderr)
        create, go, openai, execute, remove = self.trace()
        kit, workspace, sessions = create["args"][-3:]
        sandbox = create["args"][3]
        self.assertTrue(create["staged"])
        self.assertEqual(create["args"], [
            "--no-input", "create", "--name", sandbox, "--skills", "off", "--static-mcp", "none", kit, workspace,
            sessions,
        ])
        self.assertEqual(workspace, str(self.workspace))
        source, target = sessions.split(":")
        self.assertEqual(target, "/home/agent/.pi/sessions")
        self.assertEqual(Path(source).parent, self.home / ".local/state/pi-sandbox/sessions")
        self.assertEqual(Path(source).stat().st_mode & 0o777, 0o700)
        self.assertFalse(Path(kit).exists())
        provider_options = []
        for secret, host, var, resolver in (
            (go, "opencode.ai", "OPENCODE_API_KEY", ["print-api-key", "--provider", "opencode-go"]),
            (openai, "api.openai.com", "OPENAI_API_KEY",
             ["print-bearer-token", "--provider", "openai", "--min-expiry", "5m"]),
        ):
            args = secret["args"]
            placeholder = args[args.index("--placeholder") + 1]
            self.assertRegex(placeholder, r"^sbx-cs-[0-9a-f]{32}$")
            provider_options.extend(("--env", f"{var}={placeholder}"))
            self.assertEqual(args[:-1], [
                "--no-input", "secret", "set-custom", "--sandbox", sandbox, "--host", host,
                "--env", var, "--placeholder", placeholder, "--refresh", "on-demand", "--command",
            ])
            self.assertEqual(shlex.split(args[-1]), [
                "umask", "077;", "exec", "/usr/bin/env", "-i", f"PATH={os.defpath}",
                f"HOME={self.auth_root}", f"PI_CODING_AGENT_DIR={self.auth_root / 'agent'}",
                "PI_OFFLINE=1", "PI_TELEMETRY=0", "/usr/bin/node",
                str(self.auth_root / f"pi-{PI_VERSION}/node_modules/.bin/pi"), "auth", *resolver, "2>/dev/null",
            ])
        self.assertEqual(execute["args"], [
            "--no-input", "exec", "--interactive", "--user", "agent", "--workdir", workspace,
            "--env", "PI_CLIPBOARD_OSC52=1", "--env", f"PI_TASKS=/tmp/{sandbox}-tasks.json",
            "--env", "NODE_USE_ENV_PROXY=1",
            "--env", "PI_CODING_AGENT_SESSION_DIR=/home/agent/.pi/sessions",
            *provider_options,
            "--", sandbox, "/usr/local/bin/pi", "--model", "test model", "a prompt",
        ])
        self.assertEqual(execute["stdin"], "input from a pipe")
        self.assertEqual(remove["args"], ["--no-input", "rm", "--force", sandbox])
        for record in self.trace():
            self.assertNotIn("PI_TEST_SECRET", record["env"])
            self.assertNotIn("OPENCODE_API_KEY", record["env"])
            self.assertNotIn("OPENAI_API_KEY", record["env"])
            self.assertNotIn("EXA_API_KEY", record["env"])
            self.assertNotIn("SBX_CLOUD", record["env"])
            self.assertIn("DBUS_SESSION_BUS_ADDRESS", record["env"])

        self.assertEqual(self.run_launcher(input="").returncode, 0)
        self.assertNotEqual(self.trace()[5]["args"][3], sandbox)

    def test_sessions_persist_in_a_per_workspace_store(self):
        self.assertEqual(self.run_launcher("--no-auth", input="").returncode, 0)
        first = self.creates()[-1][-1]
        source = Path(first.split(":")[0])
        self.assertEqual(source.stat().st_mode & 0o777, 0o700)

        self.assertEqual(self.run_launcher("--no-auth", input="").returncode, 0)
        self.assertEqual(self.creates()[-1][-1], first)

        other = self.workspace.parent / "other project"
        other.mkdir()
        result = self.run_launcher("--no-auth", cwd=other, input="")
        self.assertEqual(result.returncode, 0, result.stderr)
        creates = [json.loads(line) for line in (other / "sbx.jsonl").read_text().splitlines()]
        self.assertNotEqual(creates[0]["args"][-1], first)

    def test_symlinked_session_store_is_refused(self):
        self.assertEqual(self.run_launcher("--no-auth", input="").returncode, 0)
        source = Path(self.creates()[-1][-1].split(":")[0])
        outside = self.home / "outside"
        outside.mkdir()
        source.rmdir()
        source.symlink_to(outside)
        (self.workspace / "sbx.jsonl").unlink()

        result = self.run_launcher("--no-auth", input="")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not a plain directory", result.stderr)
        self.assertFalse((self.workspace / "sbx.jsonl").exists())

    def test_registration_notices_are_only_shown_on_failure(self):
        result = self.run_launcher(input="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("routine registration notice", result.stderr)

        (self.workspace / "fail-auth").touch()
        result = self.run_launcher(input="")
        self.assertEqual(result.returncode, 42, result.stderr)
        self.assertIn("routine registration notice", result.stderr)
        self.assertIn("Cannot register openai", result.stderr)

    def test_failed_auth_never_runs_pi(self):
        (self.workspace / "fail-auth").touch()
        result = self.run_launcher(input="")
        self.assertEqual(result.returncode, 42, result.stderr)
        self.assertIn("pi-sandbox-auth openai", result.stderr)
        self.assertEqual([entry["args"][1] for entry in self.trace()], ["create", "secret", "secret", "rm"])

    def test_scopes_exa_to_the_vm_without_forwarding_the_key(self):
        with patch("sandbox.auth.getpass", return_value="fake-private-exa-key"):
            save_exa_credential(self.auth_root)
        self.assertEqual((self.auth_root / "exa-api-key").stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.auth_root / "agent/auth.json").read_text(), "fake-credential-store")
        result = self.run_launcher(input="")
        self.assertEqual(result.returncode, 0, result.stderr)
        create, _, _, exa, execute, _ = self.trace()
        sandbox = create["args"][3]
        self.assertEqual(exa["args"][3:9], [
            "--sandbox", sandbox, "--host", "api.exa.ai", "--env", "EXA_API_KEY",
        ])
        self.assertNotIn("fake-private-exa-key", json.dumps(self.trace()))
        placeholder = exa["args"][exa["args"].index("--placeholder") + 1]
        self.assertIn(f"EXA_API_KEY={placeholder}", execute["args"])
        self.assertNotIn("fake-private-exa-key", result.stdout + result.stderr)

        (self.workspace / "fail-exa").touch()
        result = self.run_launcher(input="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Cannot register exa", result.stderr)
        self.assertFalse(any(arg.startswith("EXA_API_KEY=") for arg in self.trace()[-2]["args"]))

    def test_auth_store_required_unless_explicitly_skipped(self):
        auth = self.auth_root / "agent/auth.json"
        auth.unlink()
        auth.symlink_to(self.workspace / "fake-auth.json")
        result = self.run_launcher(input="")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Run pi-sandbox-auth", result.stderr)
        self.assertFalse((self.workspace / "sbx.jsonl").exists())
        result = self.run_launcher("--no-auth", "--offline", input="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([entry["args"][1] for entry in self.trace()], ["create", "exec", "rm"])
        self.assertEqual(self.trace()[1]["args"][-2:], ["/usr/local/bin/pi", "--offline"])

    def test_failed_create_never_runs_pi(self):
        (self.workspace / "fail").touch()
        self.assertEqual(self.run_launcher(input="").returncode, 41)
        self.assertEqual([entry["args"][1] for entry in self.trace()], ["create", "rm"])
        self.assertFalse((self.workspace / "host-pi-ran").exists())

    def test_interruption_removes_sandbox(self):
        for signum, during in ((signal.SIGINT, "exec"), (signal.SIGTERM, "exec"), (signal.SIGTERM, "create")):
            with self.subTest(signum=signum, during=during):
                hold = self.workspace / "hold"
                hold.unlink(missing_ok=True)
                (hold if during == "exec" else self.workspace / "hold-create").touch()
                trace = self.workspace / "sbx.jsonl"
                trace.unlink(missing_ok=True)
                with subprocess.Popen(
                    [self.launcher], cwd=self.workspace, env=self.env, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                ) as launcher:
                    try:
                        deadline = time.monotonic() + 10
                        while launcher.poll() is None and time.monotonic() < deadline:
                            if trace.exists() and f'"{during}"' in trace.read_text():
                                break
                            time.sleep(0.02)
                        else:
                            self.fail(f"sandbox did not start {during}")
                        launcher.send_signal(signum)
                        _, stderr = launcher.communicate(timeout=10)
                        self.assertEqual(launcher.returncode, 128 + signum, stderr)
                    finally:
                        if launcher.poll() is None:
                            launcher.terminate()

                create, *middle, remove = self.trace()
                if during == "create":
                    self.assertEqual(middle, [])
                    self.assertTrue((self.workspace / "created").exists())
                else:
                    self.assertEqual([record["args"][1] for record in middle], ["secret", "secret", "exec"])
                self.assertEqual(remove["args"], ["--no-input", "rm", "--force", create["args"][3]])

    def test_protected_paths_fail_closed(self):
        ssh_projects = self.home / ".ssh/projects"
        ssh_projects.mkdir(parents=True)
        runtime = self.workspace / "custom runtime"
        runtime.mkdir()
        for directory in (self.home, ssh_projects, self.workspace, Path("/tmp")):
            with self.subTest(directory=directory):
                result = subprocess.run(
                    [self.launcher], cwd=directory, env={**self.env, "XDG_RUNTIME_DIR": str(runtime)},
                    capture_output=True, text=True, timeout=10,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((directory / "sbx.jsonl").exists())

    def private_launcher(self):
        fixture = self.home / "launch fixture"
        shutil.copytree(REPO, fixture, ignore=shutil.ignore_patterns("node_modules", "__pycache__", ".git"))
        config = fixture / "config/privacy-models.json"
        config.write_text(json.dumps({
            "default": "opencode-go/deepseek-v4.1-flash",
            "routes": [{
                "provider": "opencode-go", "model": "deepseek-v4.1-flash",
                "validThrough": date.max.isoformat(), "policy": "https://example.test/",
            }],
        }))
        self.launcher = fixture / "bin/pi-sandbox"

    def test_private_launcher_refuses_global_credentials(self):
        self.private_launcher()
        (self.workspace / "global-secret").touch()
        result = self.run_launcher("--privacy", input="")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no global secrets", result.stderr)
        self.assertNotIn("fake global credential name", result.stdout + result.stderr)
        self.assertEqual([record["args"] for record in self.trace()], [
            ["--no-input", "secret", "ls", "--global", "--quiet"],
        ])

    def test_private_launcher_never_mounts_the_host_session_store(self):
        self.private_launcher()
        (self.auth_root / f"pi-{PI_VERSION}/node_modules/.bin/pi").write_text('console.log("fake-go-key")\n')
        result = self.run_launcher("--privacy", input="")
        self.assertEqual(result.returncode, 0, result.stderr)
        create = next(entry["args"] for entry in self.trace() if entry["args"][1] == "create")
        execute = next(entry["args"] for entry in self.trace() if entry["args"][1] == "exec")
        self.assertEqual(create[-1], str(self.workspace))
        self.assertNotIn(str(self.home / ".local/state"), json.dumps(create))
        self.assertNotIn("--no-session", execute)
        self.assertIn("PI_CODING_AGENT_SESSION_DIR=/tmp/", json.dumps(execute))
        self.assertFalse((self.home / ".local/state/pi-sandbox/sessions").exists())

    def test_installed_link_preserves_conflicts_and_is_repeatable(self):
        agent_dir = self.home / "agent with spaces"
        installed = agent_dir / "bin/pi-sandbox"
        installed.parent.mkdir(parents=True)
        installed.write_text("old launcher")
        for _ in range(2):
            result = subprocess.run(
                [REPO / "install.sh"], env={**self.env, "PI_CODING_AGENT_DIR": str(agent_dir)},
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(installed.resolve(), self.launcher)
            self.assertEqual((agent_dir / "bin/pi-sandbox-auth").resolve(), REPO / "bin/pi-sandbox-auth")
        backups = list((agent_dir / "backups").glob("*/bin/pi-sandbox"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), "old launcher")
        self.launcher = installed
        self.assertEqual(self.run_launcher("--version", input="").returncode, 0)

    def test_privacy_denies_every_provider_the_kit_allows(self):
        loader = SourceFileLoader("pi_sandbox_launcher", str(REPO / "bin/pi-sandbox"))
        launcher = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(launcher)
        self.assertEqual(launcher.privacy_deny_hosts(), [
            "opencode.ai", "api.openai.com", "api.exa.ai", "chatgpt.com", "auth.openai.com",
        ])

        # A kit whose allow list cannot be read must refuse the launch, not deny nothing.
        launcher.REPO = Path(self.enterContext(TemporaryDirectory())) / "kit"
        (launcher.REPO / "sandbox").mkdir(parents=True)
        (launcher.REPO / "sandbox/pi.yaml").write_text("schemaVersion: \"3\"\ncapabilities: []\n")
        with self.assertRaisesRegex(ValueError, "no runtime network allow hosts"):
            launcher.privacy_deny_hosts()
