"""Verify the signed-in providers through fresh local Pi sandboxes."""

import argparse
from functools import partial
import json
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
from uuid import uuid4


REPO = Path(__file__).resolve().parent.parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(REPO))
from sandbox.auth import auth_root, credential_resolvers


DEFAULT = ("opencode-go", "deepseek-v4.1-flash")
OPENAI = ("openai", "gpt-6.1-sol")
PROMPT = "Reply with exactly the word OK."
REFRESH_KIT = "docker.io/sbx/shell@sha256:6c00b0213d78a34f39413403ef6e066bb87f1348dcc2c22e4278ca4cef5c42c4"


def sbx(*arguments):
    return subprocess.run(
        ["sbx", "--no-input", *arguments],
        stdin=subprocess.DEVNULL, capture_output=True, text=True,
    )


def require_removed(name):
    listed = sbx("ls", "--quiet")
    if listed.returncode or name in listed.stdout.splitlines():
        raise RuntimeError(f"Could not confirm removal of {name}")


def failure_kind(stderr, assistant):
    for provider in ("opencode-go", "openai"):
        if f"Cannot register {provider}." in stderr:
            return f"{provider} credential registration failed"
    # Keep provider errors private. Even an HTTP error may include auth material.
    error = (assistant.get("errorMessage") or "").lower()
    if "global" in error and ("region" in error or "privacy" in error):
        return "OpenCode Go requires Global under workspace Settings → Privacy → Regions"
    if "thinking" in error and "cannot be disabled" in error:
        return "provider requires a thinking level above off"
    if "missingsessionid" in error or "x-opencode-session" in error:
        return "OpenCode Go rejected the session header"
    for reason, words in (
        ("provider region restriction", ("region", "territory", "location", "country")),
        ("provider access or quota restriction", ("plan", "quota", "rate limit", "credits", "forbidden")),
        ("proxy or provider connection failed", ("fetch failed", "connect", "socket", "certificate", "timed out")),
        ("provider rejected authentication", ("unauthorized", "api key", "authentication")),
        ("provider rejected the request", ("bad request", "not supported", "invalid request")),
    ):
        if any(word in error for word in words):
            return reason
    status = re.search(r"\b(4[0-9]{2}|5[0-9]{2})\b", error)
    if status:
        return f"provider or proxy returned HTTP {status[1]}"
    if not assistant:
        return "Pi emitted no assistant result"
    stop = assistant.get("stopReason")
    if stop in {"stop", "error", "aborted", "length", "toolUse"}:
        return f"Pi reported stop reason {stop}"
    return "Pi reported an unexpected stop reason"


def check_inference(workspace, model, *, use_default=False, private=False):
    provider, model_id = model
    print(f"Checking {provider}/{model_id} in a fresh sandbox", flush=True)
    selection = [] if use_default else ["--provider", provider, "--model", model_id]
    command = [
        REPO / "bin/pi-sandbox", "--offline", "--no-session", "--no-tools",
        "--no-extensions", "--no-mcp", "--no-context-files", "--mode", "json",
        *selection, "-p", PROMPT,
    ]
    if private:
        command = [
            REPO / "bin/pi-sandbox", "--privacy", "--no-context-files",
            "--mode", "json", "-p",
        ]
    with subprocess.Popen(
        command, cwd=workspace, stdin=subprocess.PIPE if private else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ) as launcher:
        try:
            stdout, stderr = launcher.communicate(PROMPT if private else None, timeout=480)
        except subprocess.TimeoutExpired:
            launcher.terminate()
            launcher.communicate()
            raise RuntimeError(f"Timed out checking {provider}/{model_id}") from None
    run = re.search(r"Starting local sandbox (pi-[0-9a-f]{32}) in", stderr)
    if run:
        name = run[1]
        require_removed(name)
    assistant = {}
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "message_end" and event["message"].get("role") == "assistant":
            assistant = event["message"]
    reply = "".join(item["text"] for item in assistant.get("content", []) if item["type"] == "text")
    if not run or launcher.returncode or assistant.get("stopReason") != "stop":
        if not assistant:
            for code in ("EACCES", "ENOENT", "ERR_MODULE_NOT_FOUND", "SyntaxError", "TypeError", "ReferenceError"):
                if code in stdout + stderr:
                    print(f"Startup diagnostic: {code}", file=sys.stderr)
            for fixed in (
                "Privacy mode could not verify the approved model runtime",
                "Privacy mode requires an in-memory Pi session",
                "Invalid private model configuration", "Wrong guest runtime for privacy mode",
                "No models available", "No API key found", "Failed to load extension",
            ):
                if fixed in stdout + stderr:
                    print(f"Startup diagnostic: {fixed}", file=sys.stderr)
        raise RuntimeError(f"{provider}/{model_id} failed: {failure_kind(stderr, assistant)}")
    if (assistant.get("provider"), assistant.get("model")) != model:
        raise RuntimeError(f"Pi selected a different route than {provider}/{model_id}")
    if not reply.strip():
        raise RuntimeError(f"{provider}/{model_id} produced no text")
    print(f"{provider}/{model_id} completed in {name}; sandbox removed", flush=True)
    return name


def check_refresh():
    print("Checking host OAuth refresh through the Docker secret resolver", flush=True)
    root = auth_root()
    auth_file = root / "agent/auth.json"
    name = f"pi-provider-refresh-{uuid4().hex}"
    _, host, variable, command = next(entry for entry in credential_resolvers(root) if entry[0] == "openai")
    forced = command.replace("--min-expiry 5m", "--min-expiry 8760h")
    if forced == command:
        raise RuntimeError("OpenAI resolver's minimum validity flag changed")
    try:
        if sbx("create", "--name", name, "--skills", "off", "--static-mcp", "none", REFRESH_KIT).returncode:
            raise RuntimeError("Could not create the refresh test sandbox")
        before = auth_file.stat().st_mtime_ns
        # Pi persists a refreshed token under its lock before testing the requested
        # lifetime. A one-year minimum forces early refresh, then fails validation.
        refresh = sbx(
            "secret", "set-custom", "--sandbox", name, "--host", host, "--env", variable,
            "--placeholder", f"sbx-cs-{uuid4().hex}", "--command", forced,
        )
        if refresh.returncode == 0 or auth_file.stat().st_mtime_ns == before:
            raise RuntimeError("OAuth refresh did not show the expected credential store update")
    finally:
        sbx("rm", "--force", name)
        require_removed(name)
    print("Host OAuth credential store updated; refresh test sandbox removed", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", choices=("go", "openai", "privacy"), help="verify one provider or the private relay")
    arguments = parser.parse_args()
    failures = 0
    names = set()
    with TemporaryDirectory(prefix="pi-provider-test-", dir="/tmp") as workspace:
        checks = []
        if arguments.only == "privacy":
            checks.append(partial(check_inference, workspace, DEFAULT, private=True))
        elif arguments.only != "openai":
            checks.append(partial(check_inference, workspace, DEFAULT, use_default=True))
        if arguments.only not in {"go", "privacy"}:
            openai = partial(check_inference, workspace, OPENAI)
            checks.extend((openai, check_refresh, openai))
        for operation in checks:
            try:
                name = operation()
                if name in names:
                    raise RuntimeError("Provider checks reused a sandbox")
                if name:
                    names.add(name)
            except RuntimeError as error:
                print(error, file=sys.stderr, flush=True)
                failures += 1
    if failures:
        raise RuntimeError(f"{failures} provider checks failed")
    print("Selected provider checks passed.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError) as error:
        sys.exit(str(error))
