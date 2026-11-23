# Verification

Dry-run the installer against a temporary agent directory:

```sh
test_dir="$(mktemp -d)"
PI_CODING_AGENT_DIR="$test_dir" ./install.sh
PI_CODING_AGENT_DIR="$test_dir" ./install.sh
```

This checks linking and repeatability only; it does not launch Pi or authenticate.

Run these from the checkout. Sandbox checks need host `sbx` access and remove
their VMs.

| Check | What it verifies |
| --- | --- |
| `python3 -B -m unittest discover -s tests -v` | Installer conflicts/repeatability, staging exclusions, launcher failures, relay rejection and expiration |
| `bash sandbox/check.sh` | Guest build, pinned versions, loaded guidance and extensions; no inference |
| `bash sandbox/check.sh --privacy` | Private SDK, model catalog, ephemeral guest state; synthetic credentials |
| `python3 -B tests/check_launcher.py` | Fresh VM isolation, workspace writes, parent/child context and interruption; no inference |
| `python3 -B tests/check_providers.py` | Small real Go/OpenAI requests and forced OAuth refresh |
| `python3 -B tests/check_providers.py --only privacy` | Small real Go request through the private relay |
| `python3 -B tests/check_tools.py` | Real concurrent children, steering, shared tasks/progress, Exa search and page fetching |
| `python3 -B tests/check_privacy.py` | Real private TUI with synthetic provider responses; model picker, shared tasks, ephemeral guest writes and live host/guest file monitoring, including interruption |
| `python3 -B tests/check_bootstrap.py` | Clean source snapshot, temporary installation twice, and a fresh VM launched through the installed link; no inference |

The local unit tests pass on the current tree. The live checks below were last
run on November 22, 2026, before the privacy-mode simplification and must be
rerun.

The privacy audit sends its unique marker through stdin and keeps terminal output
and traces in memory. It monitors guest home, log, and workspace paths; guest
`/tmp` and the private agent directory are treated as ephemeral state that may be
written. It also watches selected Docker daemon, client, container, MCP, crash,
and audit logs on the host. Requested workspace artifacts use separate contents. It checks writes
while the VM runs, before removal, and host logs after removal. These are
regression checks for the pinned implementation, not independent verification of
provider retention.
