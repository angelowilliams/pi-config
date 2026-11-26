#!/usr/bin/env bash
set -euo pipefail
export SBX_NO_TELEMETRY=1

kit_source="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
scratch="$(mktemp -d)"
name="pi-guest-${scratch##*.}"
attempted=false
cleanup() {
    local status=$?
    if "$attempted"; then
        if ! sbx rm --force "$name"; then
            printf 'Cleanup could not confirm removal of sandbox %s; check sbx ls.\n' "$name" >&2
            status=1
        fi
    fi
    rm -rf -- "$scratch"
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

kit="$scratch/pi"
pi_version="$(PYTHONPATH="$kit_source/.." python3 -B -c 'import sandbox.auth; print(sandbox.auth.PI_VERSION)')"
if [[ ! "$pi_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    printf 'Unexpected Pi version from sandbox/auth.py: %s\n' "$pi_version" >&2
    exit 1
fi
python3 "$kit_source/stage.py" "$kit"
attempted=true
SBX_KIT_BUILDER=sandbox sbx create --name "$name" --skills off --static-mcp none "$kit"

if [[ "${1:-}" == --privacy ]]; then
    sbx exec --interactive --user agent --workdir /home/agent \
        --env PI_OFFLINE=1 --env PI_SANDBOX_PRIVACY=1 \
        --env PI_PRIVACY_BASE_URL=http://host.docker.internal:1/v1 --env PI_PRIVACY_RELAY_TOKEN=synthetic-check \
        --env PI_CODING_AGENT_DIR=/opt/pi/private-agent \
        --env PI_TASKS=/tmp/check-tasks.json --env PI_CODING_AGENT_SESSION_DIR=/tmp/check-sessions \
        --env TMPDIR=/tmp --env TMP=/tmp --env TEMP=/tmp \
        "$name" node --input-type=module - < "$kit_source/../tests/privacy_probe.mjs"
    exit
fi

sbx exec --user agent --workdir /home/agent "$name" bash -eu -c '
    test "$HOME" = /home/agent
    agent_dir="$HOME/.pi/agent"
    for path in auth.json mcp-auth.json sessions backups models.json; do
        test ! -e "$agent_dir/$path"
    done
    test "$(pi --version)" = '"$pi_version"'
    pi --version
    node --version
    git --version
    fd --version
    rg --version
    python3 --version
    pi list
'

sbx exec --interactive --user agent --workdir /home/agent --env PI_OFFLINE=1 \
    --env OPENCODE_API_KEY=sbx-cs-test-go --env OPENAI_API_KEY=sbx-cs-test-openai "$name" \
    node --input-type=module - <<'JS'
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { createAgentSession, DefaultResourceLoader, SessionManager } from "/opt/pi/node_modules/@earendil-works/pi-coding-agent/dist/index.js";

const cwd = "/home/agent";
const agentDir = join(cwd, ".pi/agent");
const loader = new DefaultResourceLoader({ cwd, agentDir });
await loader.reload();
const { session } = await createAgentSession({
    cwd, agentDir, resourceLoader: loader, sessionManager: SessionManager.inMemory(),
});
try {
    const settings = JSON.parse(readFileSync(join(agentDir, "settings.json"), "utf8"));
    assert.equal(session.model?.provider, settings.defaultProvider);
    assert.equal(session.model?.id, settings.defaultModel);
    console.log("Selected default:", `${session.model.provider}/${session.model.id}`);
    const guidance = join(agentDir, "AGENTS.md");
    assert(loader.getAgentsFiles().agentsFiles.some(({ path }) => path === guidance));
    assert(session.systemPrompt.includes(readFileSync(guidance, "utf8").trim()));
    const resources = loader.getExtensions();
    assert.equal(resources.errors.length, 0, "Pi reported extension loading errors");
    console.log("Loaded guidance:", guidance);
    console.log("Loaded extensions:", resources.extensions.map(({ path }) => path));
    console.log("Loaded skills:", loader.getSkills().skills.map(({ name }) => name));
    console.log("Loaded prompts:", loader.getPrompts().prompts.map(({ name }) => name));
} finally {
    session.dispose();
}
JS
