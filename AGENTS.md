# Maintaining this setup

This repository owns personal Pi configuration and custom resources.

- Keep the setup small. Add capabilities only when requested.
- Every change should be as minimal as possible.
- Changes to markdown files always require explicit user approval.
- Edit repo files directly; `install.sh` links them into Pi's agent directory.
- Keep external Pi packages in `config/settings.json`, pinned to exact versions or commits.
- Never commit credentials, session history, caches, or installed dependencies.
- Keep the installer idempotent.
- Test installer changes using a temporary `PI_CODING_AGENT_DIR`, never the live setup.

This file guides maintenance of this repository. To add instructions used across
all projects, create `config/AGENTS.md` and rerun the installer.
