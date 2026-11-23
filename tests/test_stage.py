import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sandbox.auth import INSTALL_FILES, PI_VERSION, RELEASE
from sandbox.stage import KIT_FILES, stage


class StageTests(unittest.TestCase):
    def setUp(self):
        scratch = self.enterContext(TemporaryDirectory())
        self.repo = Path(scratch) / "working files"
        self.destination = Path(scratch) / "staged kit"
        for directory in ("config", "sandbox", "skills", "extensions", "prompts"):
            (self.repo / directory).mkdir(parents=True)
        for path in (*(f"sandbox/{name}" for name in KIT_FILES), "config/AGENTS.md"):
            (self.repo / path).write_text(path, encoding="utf-8")
        (self.repo / "config/subagents.json").write_text('{"widgetMode":"all"}', encoding="utf-8")
        (self.repo / "config/privacy-models.json").write_text(
            '{"default":"opencode-go/deepseek-v4.1-flash"}', encoding="utf-8",
        )
        (self.repo / "config/settings.json").write_text(
            json.dumps({
                "defaultProvider": "opencode-go",
                "defaultModel": "deepseek-v4.1-flash",
                "packages": ["npm:package@1.0.0"],
                "providerEnv": {"opencode-go": {"OPENCODE_API_KEY": "fake-key"}},
            }),
            encoding="utf-8",
        )

    def test_stages_selected_working_files_without_git(self):
        skill = self.repo / "skills/example"
        skill.mkdir()
        (skill / "SKILL.md").write_text("test skill", encoding="utf-8")
        tool = skill / "tool.sh"
        tool.write_text("#!/bin/sh\n", encoding="utf-8")
        tool.chmod(0o755)
        (self.repo / "skills/alias").symlink_to("example")
        (self.repo / "prompts/example.md").symlink_to("../skills/example/SKILL.md")
        for path in (".env", "auth.json", "history.jsonl", "private.key", "stale.pyc"):
            (skill / path).write_text("test state", encoding="utf-8")
        (skill / "helper.py").write_text("print('helper')\n", encoding="utf-8")
        (skill / "__pycache__").mkdir()
        (skill / "__pycache__/helper.cpython-312.pyc").write_text("test cache", encoding="utf-8")
        (self.repo / "config/models.json").write_text("test state", encoding="utf-8")
        (skill / "sessions").mkdir()
        (skill / "sessions/transcript").write_text("test state", encoding="utf-8")

        stage(self.repo, self.destination)

        files = {
            str(path.relative_to(self.destination))
            for path in self.destination.rglob("*")
            if path.is_file()
        }
        self.assertEqual(files, {
            *KIT_FILES, "privacy-models.json", "agent/AGENTS.md", "agent/settings.json",
            "agent/subagents.json",
            "agent/skills/example/SKILL.md", "agent/skills/example/tool.sh",
            "agent/skills/example/helper.py",
            "agent/skills/alias/SKILL.md", "agent/skills/alias/tool.sh",
            "agent/skills/alias/helper.py",
            "agent/prompts/example.md",
            "private-agent/AGENTS.md", "private-agent/subagents.json", "private-agent/settings.json",
            "private-agent/keybindings.json", "private-agent/extensions/privacy-child.ts",
        })
        self.assertFalse(any(path.is_symlink() for path in self.destination.rglob("*")))
        self.assertEqual((self.destination / "agent/skills/alias/tool.sh").stat().st_mode & 0o777, 0o755)
        settings = json.loads((self.destination / "agent/settings.json").read_text())
        self.assertEqual(settings, {
            "defaultProvider": "opencode-go",
            "defaultModel": "deepseek-v4.1-flash",
            "packages": ["npm:package@1.0.0"],
        })
        private = self.destination / "private-agent"
        self.assertEqual((private / "AGENTS.md").read_bytes(), (self.repo / "config/AGENTS.md").read_bytes())
        private_settings = json.loads((private / "settings.json").read_text())
        self.assertEqual(private_settings["enabledModels"], ["opencode-go/deepseek-v4.1-flash"])
        self.assertEqual(private_settings["packages"], [])
        self.assertFalse(private_settings["enableAnalytics"])
        self.assertEqual(private_settings["cacheWarming"], "off")
        keybindings = json.loads((private / "keybindings.json").read_text())
        self.assertEqual(keybindings, {"app.editor.external": []})
        private_subagents = json.loads((private / "subagents.json").read_text())
        self.assertEqual(private_subagents, {
            "widgetMode": "all", "schedulingEnabled": False, "workflowsEnabled": False,
        })

    def test_renders_the_pinned_release_into_the_staged_dockerfile(self):
        (self.repo / "sandbox/pi.dockerfile").write_text(
            "ADD --checksum=sha256:{{SHA256_PACKAGE_JSON}} {{RELEASE}}/pi-coding-agent-install-package.json /opt/pi/package.json\n"
            "ADD --checksum=sha256:{{SHA256_PACKAGE_LOCK_JSON}} {{RELEASE}}/pi-coding-agent-install-package-lock.json /opt/pi/package-lock.json\n"
            'RUN test "$(pi --version)" = {{PI_VERSION}}\n',
            encoding="utf-8",
        )

        stage(self.repo, self.destination)

        self.assertEqual(
            (self.destination / "pi.dockerfile").read_text(encoding="utf-8"),
            f"ADD --checksum=sha256:{INSTALL_FILES['package.json']} {RELEASE}/pi-coding-agent-install-package.json /opt/pi/package.json\n"
            f"ADD --checksum=sha256:{INSTALL_FILES['package-lock.json']} {RELEASE}/pi-coding-agent-install-package-lock.json /opt/pi/package-lock.json\n"
            f'RUN test "$(pi --version)" = {PI_VERSION}\n',
        )

    def test_rejects_an_unknown_dockerfile_placeholder(self):
        (self.repo / "sandbox/pi.dockerfile").write_text("RUN echo {{UNKNOWN}}\n", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "unknown release placeholder"):
            stage(self.repo, self.destination)

    def test_rejects_symlink_escape(self):
        outside = self.repo.parent / "outside.md"
        outside.write_text("test state", encoding="utf-8")
        (self.repo / "prompts/outside.md").symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "leaves the checkout"):
            stage(self.repo, self.destination)

    def test_rejects_links_to_excluded_state_or_other_directories(self):
        prompt = self.repo / "prompts/alias.md"
        (self.repo / "skills/auth.json").write_text("test state", encoding="utf-8")
        prompt.symlink_to("../skills/auth.json")
        with self.assertRaisesRegex(ValueError, "excluded state"):
            stage(self.repo, self.destination)

        prompt.unlink()
        prompt.symlink_to("../config/AGENTS.md")
        with self.assertRaisesRegex(ValueError, "leaves the selected resource directories"):
            stage(self.repo, self.repo.parent / "another kit")

    def test_rejects_circular_directory_links(self):
        (self.repo / "skills/loop").symlink_to(".")
        with self.assertRaisesRegex(ValueError, "circular resource directory"):
            stage(self.repo, self.destination)
