"""Install from a clean source snapshot and launch through its installed entry point."""

import os
from pathlib import Path
import re
import shutil
import subprocess
from tempfile import TemporaryDirectory


REPO = Path(__file__).resolve().parent.parent


def main():
    with TemporaryDirectory(prefix="pi-bootstrap-", dir="/tmp") as directory:
        root = Path(directory)
        checkout = root / "clean checkout"
        checkout.mkdir()
        for name in ("config", "bin", "sandbox", "skills", "extensions", "prompts"):
            shutil.copytree(REPO / name, checkout / name, symlinks=True)
        shutil.copy2(REPO / "install.sh", checkout)
        agent = root / "temporary Pi agent"
        env = {**os.environ, "PI_CODING_AGENT_DIR": str(agent)}
        for _ in range(2):
            installed = subprocess.run([checkout / "install.sh"], env=env, capture_output=True, text=True)
            if installed.returncode:
                raise RuntimeError("Clean temporary installation failed")
        if (agent / "backups").exists():
            raise RuntimeError("Repeated clean installation unexpectedly created a backup")
        if (agent / "bin/pi-sandbox").resolve() != checkout / "bin/pi-sandbox":
            raise RuntimeError("Installed launcher does not resolve to the clean snapshot")
        workspace = root / "fresh project"
        workspace.mkdir()
        with subprocess.Popen(
            [agent / "bin/pi-sandbox", "--no-auth", "--offline", "--version"],
            cwd=workspace, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ) as run:
            try:
                stdout, stderr = run.communicate(timeout=480)
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                run.terminate()
                run.communicate()
                raise
        match = re.search(r"Starting local sandbox (pi-[0-9a-f]{32}) in", stderr)
        if run.returncode or not match or not re.search(r"^1\.6\.0$", stdout, re.M):
            raise RuntimeError("Fresh VM launch from the installed clean snapshot failed")
        listed = subprocess.run(["sbx", "--no-input", "ls", "--quiet"],
                                capture_output=True, text=True, timeout=30)
        if listed.returncode or match[1] in listed.stdout.splitlines():
            raise RuntimeError("Clean bootstrap VM removal could not be confirmed")
        print("Clean snapshot installed twice; installed launcher ran Pi 1.6.0 in a fresh VM and removed it.")


if __name__ == "__main__":
    main()
