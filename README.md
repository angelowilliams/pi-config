# My Pi config

Personal Pi configuration, skills, extensions, and prompt templates.

The goal of this repo is to take a vanilla Pi install and have it fully
configured for my workflows

## Usage

```sh
pi-sandbox              # start Pi in a new sandbox for this directory
pi-sandbox --continue   # resume the last session for this directory
pi-sandbox --resume     # choose a saved session for this directory
pi-sandbox --privacy    # start a private session; state is discarded on exit
```

## Prerequisites

- Linux x86_64 host with KVM accessible to your user.
- Docker Sandboxes (`sbx`).
- Python 3.11+.
- `curl` at `/usr/bin/curl`.
- Node.js 22.19+ with npm at `/usr/bin/node` and `/usr/bin/npm`.
- A OpenAI subscription.
- A OpenCode Go subscription.

## Setup

These steps assume the prerequisites above are met.

1. Sign in to Docker with `sbx login`. At the first-run network-policy prompt,
   choose **Open**.
2. Configure Docker Sandboxes, restart the daemon, and check the settings:

   ```sh
   sbx settings set telemetry.disabled true
   sbx settings set diagnostics.autoUpload no
   sbx settings set clipboard.textCopy true
   sbx daemon restart
   sbx settings get telemetry.disabled
   sbx settings get diagnostics.autoUpload
   sbx settings get clipboard.textCopy
   ```
3. Close running Pi sessions, then run `./install.sh` from this repo.
4. Run `~/.pi/agent/bin/pi-sandbox-auth`. Complete the browser sign-in with the
   existing ChatGPT Pro account and enter your OpenCode Go API key.
5. Run `~/.pi/agent/bin/pi-sandbox-auth exa` and enter your existing Exa API key.
6. Add the optional alias below to your `~/.bashrc`:
```bash
alias pi-sandbox="$HOME/.pi/agent/bin/pi-sandbox"
```

## Project organization

| Repo path | Purpose |
| --- | --- |
| `config/` | Files linked into `~/.pi/agent`, currently settings and model routing |
| `skills/<name>/SKILL.md` | Task-specific guidance, with optional references and scripts |
| `extensions/` | Custom TypeScript or JavaScript behavior |
| `prompts/<name>.md` | Reusable slash-command prompts |
| `bin/` | Sandbox launcher and host authentication entry point |
| `sandbox/` | Pinned guest kit, packaging, and host auth helpers |
| `tests/` | Installer, packaging, launcher, and live isolation checks |
| `VERIFICATION.md` | How to run the tests and live checks |
| `AGENTS.md` | Instructions for maintaining this repository |

## Privacy mode

Start a private VM from your project directory:

```sh
~/.pi/agent/bin/pi-sandbox --privacy
```

Privacy mode minimizes the data vendors receive and store and the session data
kept locally. Its only inference route, `opencode-go/deepseek-v4.1-flash`, is
covered by Go's
[published privacy policy](https://opencode.ai/docs/console/go/#privacy). The controls cover Pi's inference and automatic
conversation logging, not arbitrary network traffic, terminal recording,
workspace writes, or host backups.

What changes:
- Custom extensions and MCP servers are disabled.
- The terminal shows `Privacy mode` throughout the session.
- `config/privacy-models.json` allows only `opencode-go/deepseek-v4.1-flash`.
- The host relay rejects models outside the whitelist, and inherited global
  Docker secrets are refused.
- Web search and page fetching are unavailable, and no Exa or OpenAI credential
  is registered with the VM.
- Sessions, child transcripts, and task state live in the VM's ephemeral storage
  and are removed with it; private workflows and scheduled runs are disabled.
- Parent and child sessions share one task list in ephemeral guest storage.
- Debug and crash files stay in the VM and are discarded with it, and the
  external editor is disabled.
- Core dumps are disabled in the relay and the guest.

