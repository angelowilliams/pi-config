#!/usr/bin/env python3
"""Stage a local sbx kit using repository config, skills, extensions, and prompts."""

import argparse
from fnmatch import fnmatchcase
import importlib.util
import json
from pathlib import Path
import shutil
import sys


KIT_FILES = (
    "pi.yaml", "pi.dockerfile",
    "privacy.ts", "privacy-child.ts", "privacy-runtime.ts",
)
RESOURCE_DIRS = ("skills", "extensions", "prompts")
# Case-insensitive patterns for one path part: hidden entries, state, and caches.
STATE_PATTERNS = (
    ".*", "auth.json", "mcp-auth.json", "node_modules", "sessions", "backups", "cache",
    "caches", "credentials", "credentials.json", "secrets", "secrets.json", "tokens.json",
    "*.jsonl", "*.local.json", "*.log", "*.pem", "*.key", "*.p12", "*.pfx",
    "__pycache__", "*.pyc",
)


def excluded(path):
    return any(
        fnmatchcase(part.lower(), pattern)
        for part in path.parts
        for pattern in STATE_PATTERNS
    )


def release_pin():
    # auth.py owns the pinned release. Load it by path: bin/pi-sandbox runs this
    # script with python3 -I, which does not put the script directory on sys.path.
    # Loading by path would otherwise leave auth's bytecode in the checkout.
    sys.dont_write_bytecode = True
    path = Path(__file__).resolve().parent / "auth.py"
    spec = importlib.util.spec_from_file_location("pi_release_pin", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def staged_dockerfile(text):
    pin = release_pin()
    replacements = {
        "{{PI_VERSION}}": pin.PI_VERSION,
        "{{RELEASE}}": pin.RELEASE,
        "{{SHA256_PACKAGE_JSON}}": pin.INSTALL_FILES["package.json"],
        "{{SHA256_PACKAGE_LOCK_JSON}}": pin.INSTALL_FILES["package-lock.json"],
    }
    for placeholder, value in replacements.items():
        text = text.replace(placeholder, value)
    if "{{" in text:
        raise ValueError("pi.dockerfile has an unknown release placeholder")
    return text


def stage(repo, destination):
    if any(destination.resolve().is_relative_to(repo / name) for name in RESOURCE_DIRS):
        raise ValueError("staged kit must be outside the resource directories")

    def source_path(relative):
        path = (repo / relative).resolve(strict=True)
        if not path.is_relative_to(repo):
            raise ValueError(f"resource leaves the checkout: {relative}")
        relative_target = path.relative_to(repo)
        if excluded(relative_target):
            raise ValueError(f"resource resolves to excluded state: {relative}")
        return path

    def config_file(relative):
        path = source_path(relative)
        if path != repo / relative or not path.is_file():
            raise ValueError(f"expected a regular repository file: {relative}")
        return path

    def copy_resource(relative, output, ancestors=()):
        if excluded(relative):
            return
        path = source_path(relative)
        target = path.relative_to(repo)
        if not target.parts or target.parts[0] not in RESOURCE_DIRS:
            raise ValueError(f"resource leaves the selected resource directories: {relative}")
        if path.is_dir():
            if path in ancestors:
                raise ValueError(f"circular resource directory: {relative}")
            output.mkdir()
            for child in sorted(path.iterdir()):
                copy_resource(target / child.name, output / child.name, (*ancestors, path))
        else:
            if not path.is_file():
                raise ValueError(f"resource must be a regular file: {relative}")
            shutil.copy(path, output)

    agent = destination / "agent"
    destination.mkdir()
    agent.mkdir()
    for filename in KIT_FILES:
        source = config_file(Path("sandbox") / filename)
        if filename == "pi.dockerfile":
            rendered = staged_dockerfile(source.read_text(encoding="utf-8"))
            (destination / filename).write_text(rendered, encoding="utf-8")
        else:
            shutil.copy(source, destination)
    for filename in ("AGENTS.md", "subagents.json"):
        shutil.copy(config_file(Path("config") / filename), agent / filename)

    settings = json.loads(config_file(Path("config/settings.json")).read_text())
    staged_settings = {key: settings[key] for key in ("defaultProvider", "defaultModel") if key in settings}
    staged_settings["packages"] = settings.get("packages", [])
    with (agent / "settings.json").open("w") as output:
        json.dump(staged_settings, output, indent=2)
        output.write("\n")

    model_policy = config_file(Path("config/privacy-models.json"))
    shutil.copy(model_policy, destination / model_policy.name)
    private_agent = destination / "private-agent"
    private_agent.mkdir()
    extensions = private_agent / "extensions"
    extensions.mkdir()
    shutil.copy(destination / "privacy-child.ts", extensions)
    shutil.copy(agent / "AGENTS.md", private_agent)
    private_subagents = json.loads((agent / "subagents.json").read_text())
    private_subagents.update({"schedulingEnabled": False, "workflowsEnabled": False})
    (private_agent / "subagents.json").write_text(json.dumps(private_subagents, indent=2) + "\n")
    private_model = json.loads(model_policy.read_text())["default"]
    provider, model = private_model.split("/", 1)
    private_settings = {
        "defaultProvider": provider,
        "defaultModel": model,
        "enabledModels": [private_model],
        "packages": [],
        "cacheWarming": "off",
        "enableInstallTelemetry": False,
        "enableAnalytics": False,
    }
    private_keybindings = {"app.editor.external": []}
    for filename, value in (("settings.json", private_settings), ("keybindings.json", private_keybindings)):
        (private_agent / filename).write_text(json.dumps(value, indent=2) + "\n")

    for name in RESOURCE_DIRS:
        relative = Path(name)
        source = repo / relative
        if source.exists() or source.is_symlink():
            copy_resource(relative, agent / name)
        else:
            (agent / name).mkdir()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path, help="new directory for the staged kit")
    arguments = parser.parse_args()
    repo = Path(__file__).resolve().parent.parent
    try:
        stage(repo, arguments.destination)
    except (OSError, RuntimeError, ValueError) as error:
        parser.exit(1, f"Cannot stage Pi kit: {error}\n")


if __name__ == "__main__":
    main()
