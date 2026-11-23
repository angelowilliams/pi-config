"""Install and invoke the private host Pi authentication runtime."""

import argparse
import errno
from getpass import GetPassWarning, getpass
from hashlib import sha256
import os
from pathlib import Path
import shlex
from stat import S_ISREG
import subprocess
from tempfile import TemporaryDirectory
import warnings


# Single source of truth for the pinned Pi release: stage.py renders the guest
# dockerfile from these values and check.sh reads PI_VERSION from here.
PI_VERSION = "1.6.0"
RELEASE = f"https://github.com/earendil-works/pi/releases/download/v{PI_VERSION}"
INSTALL_FILES = {
    "package.json": "4b61ca991909e436adf8238af90e865314dd32354d58f2e20ebfa8cc419e1baf",
    "package-lock.json": "11fa077f9cde7b5b2b6c1341dd31dbf1bc88ce5e432c1ad7cd5f84618259d2dd",
}
NODE = "/usr/bin/node"
PROVIDERS = {
    "opencode-go": ("opencode.ai", "OPENCODE_API_KEY", "print-api-key"),
    "openai": ("api.openai.com", "OPENAI_API_KEY", "print-bearer-token"),
}


def auth_root():
    return Path.home().resolve() / ".local/share/pi-sandbox/auth-runtime"


def private_directory(path):
    if path.resolve() != path or not path.is_dir():
        raise ValueError(f"Expected a private directory at {path}")
    metadata = path.stat()
    if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
        raise ValueError(f"Expected user-only access to {path}")


def pi_command(runtime):
    private_directory(runtime)
    cli = (runtime / "node_modules/.bin/pi").resolve()
    if not cli.is_relative_to(runtime) or not cli.is_file():
        raise ValueError(f"Missing Pi {PI_VERSION} authentication CLI in {runtime}")
    return [NODE, str(cli)]


def pi_env(root, agent):
    return {
        "PATH": os.defpath,
        "HOME": str(root),
        "PI_CODING_AGENT_DIR": str(agent),
        "PI_OFFLINE": "1",
        "PI_TELEMETRY": "0",
    }


def credential_directory(root, *, create=False):
    agent = root / "agent"
    if create and not agent.is_symlink():
        agent.mkdir(mode=0o700, exist_ok=True)
    private_directory(agent)
    auth_file = agent / "auth.json"
    try:
        metadata = auth_file.lstat()
    except FileNotFoundError:
        if create:
            return agent
    else:
        if S_ISREG(metadata.st_mode) and metadata.st_mode & 0o077 == 0 and metadata.st_uid == os.getuid():
            return agent
    raise ValueError(f"Expected a private Pi credential store at {auth_file}")


def prepare(root):
    if root.resolve() != root:
        raise ValueError(f"Authentication runtime path must not traverse symlinks: {root}")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    private_directory(root)
    runtime = root / f"pi-{PI_VERSION}"
    with TemporaryDirectory(prefix="prepare-", dir=root) as scratch:
        scratch = Path(scratch)
        env = pi_env(scratch, scratch / "agent")
        if not runtime.is_dir():
            install = scratch / "runtime"
            install.mkdir(mode=0o700)
            for filename, checksum in INSTALL_FILES.items():
                destination = install / filename
                subprocess.run(
                    ["/usr/bin/curl", "-q", "-fsSL", "--proto", "=https", "--proto-redir", "=https",
                     "--output", destination, f"{RELEASE}/pi-coding-agent-install-{filename}"],
                    cwd=scratch, env=env, check=True,
                )
                if sha256(destination.read_bytes()).hexdigest() != checksum:
                    raise ValueError(f"Pi release checksum mismatch for {filename}")
            user_npmrc, global_npmrc = (scratch / name for name in ("user-npmrc", "global-npmrc"))
            user_npmrc.touch()
            global_npmrc.touch()
            subprocess.run(
                [NODE, "/usr/bin/npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund",
                 "--userconfig", user_npmrc, "--globalconfig", global_npmrc, "--cache", scratch / "npm"],
                cwd=install, env=env, check=True,
            )
            verify_version(install, scratch, env)
            try:
                install.rename(runtime)
            except OSError as error:
                if error.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                    raise
        verify_version(runtime, scratch, env)
    return runtime


def verify_version(runtime, cwd, env):
    version = subprocess.check_output([*pi_command(runtime), "--version"], cwd=cwd, env=env, text=True)
    if version.strip() != PI_VERSION:
        raise ValueError(f"Authentication runtime must be Pi {PI_VERSION}")


def credential_resolvers(root):
    private_directory(root)
    agent = credential_directory(root)
    command = pi_command(root / f"pi-{PI_VERSION}")
    env = [f"{key}={value}" for key, value in pi_env(root, agent).items()]

    for provider, (host, variable, action) in PROVIDERS.items():
        arguments = ["/usr/bin/env", "-i", *env, *command, "auth", action, "--provider", provider]
        if provider == "openai":
            arguments.extend(("--min-expiry", "5m"))
        # The daemon captures stdout as the credential. Suppress resolver diagnostics.
        yield provider, host, variable, f"umask 077; exec {shlex.join(arguments)} 2>/dev/null"

    exa_key = exa_credential_path(root)
    if exa_key is not None:
        command = shlex.join(("/usr/bin/cat", "--", str(exa_key)))
        yield "exa", "api.exa.ai", "EXA_API_KEY", f"exec {command} 2>/dev/null"


def exa_credential_path(root):
    path = root / "exa-api-key"
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    if not S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_uid != os.getuid():
        raise ValueError(f"Expected a private Exa credential file at {path}")
    return path


def save_exa_credential(root):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    private_directory(root)
    exa_credential_path(root)
    with warnings.catch_warnings():
        warnings.simplefilter("error", GetPassWarning)
        try:
            key = getpass("Exa API key: ").strip()
        except GetPassWarning as error:
            raise ValueError("Cannot disable terminal echo for the Exa key") from error
    if not key or any(character.isspace() for character in key):
        raise ValueError("Enter a non-empty Exa API key without spaces")
    with TemporaryDirectory(prefix="exa-", dir=root) as scratch:
        key_file = Path(scratch) / "key"
        key_file.touch(mode=0o600, exist_ok=False)
        key_file.write_text(key)
        key_file.replace(root / "exa-api-key")


def main():
    parser = argparse.ArgumentParser(description="Set up host credentials for pi-sandbox.")
    parser.add_argument("provider", choices=("all", *PROVIDERS, "exa"), nargs="?", default="all")
    parser.add_argument("--prepare", action="store_true", help="install the pinned CLI without signing in")
    arguments = parser.parse_args()
    if not arguments.prepare and not (os.isatty(0) and os.isatty(1)):
        parser.exit(1, "Run pi-sandbox-auth in a host terminal to sign in interactively.\n")

    os.umask(0o077)
    root = auth_root()
    try:
        if arguments.provider == "exa" and not arguments.prepare:
            save_exa_credential(root)
            print("Exa API key saved on the host.")
            return
        runtime = prepare(root)
        if arguments.prepare:
            print(f"Pi {PI_VERSION} authentication runtime ready at {runtime}")
            return
        agent = credential_directory(root, create=True)
        env = pi_env(root, agent)
        # The interactive sign-in can open the user's browser through the desktop session.
        for key in ("DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR", "XAUTHORITY"):
            if key in os.environ:
                env[key] = os.environ[key]
        env["HOME"] = str(Path.home())
        providers = ("openai", "opencode-go") if arguments.provider == "all" else (arguments.provider,)
        for provider in providers:
            command = [*pi_command(runtime), "auth", "login", provider]
            if provider == "opencode-go":
                command.append("--api-key")
            subprocess.run(command, cwd=root, env=env, check=True)
        print("Pi sandbox authentication saved on the host.")
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Cannot set up Pi sandbox authentication: {error}\n")
    except KeyboardInterrupt:
        parser.exit(130, "Pi sandbox authentication interrupted.\n")
