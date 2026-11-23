#!/usr/bin/env bash
set -euo pipefail
shopt -s nullglob

repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
agent_dir="${PI_CODING_AGENT_DIR:-$HOME/.pi/agent}"
backup_dir=""

# Refuse to install into the checkout: linking would move repository files into
# a backup and leave self-referential links behind.
agent_dir="$(realpath -m -- "$agent_dir")"
if [[ "$agent_dir" == "$repo_dir" || "$agent_dir" == "$repo_dir"/* ]]; then
    printf 'Refusing to install into the repository: agent dir %s is inside %s\n' \
        "$agent_dir" "$repo_dir" >&2
    exit 1
fi

mkdir -p -- "$agent_dir"
agent_dir="$(cd -- "$agent_dir" && pwd -P)"

link_file() {
    local source="$1" destination="$2" relative_path

    if [[ -L "$destination" ]] && [[ "$(readlink -- "$destination")" == "$source" ]]; then
        printf 'Already linked: %s\n' "$destination"
        return
    fi

    if [[ -e "$destination" || -L "$destination" ]]; then
        if [[ -z "$backup_dir" ]]; then
            mkdir -p -- "$agent_dir/backups"
            backup_dir="$(mktemp -d "$agent_dir/backups/agents-XXXXXXXX")"
        fi
        relative_path="${destination#"$agent_dir/"}"
        mkdir -p -- "$(dirname -- "$backup_dir/$relative_path")"
        mv -- "$destination" "$backup_dir/$relative_path"
        printf 'Backed up: %s -> %s\n' "$destination" "$backup_dir/$relative_path"
    fi

    mkdir -p -- "$(dirname -- "$destination")"
    ln -s -- "$source" "$destination"
    printf 'Linked: %s -> %s\n' "$destination" "$source"
}

# Drop links this installer left behind for repository files that no longer exist.
remove_stale_links() {
    local root="$1" link target

    for link in "$root"/*; do
        if [[ ! -L "$link" ]]; then
            continue
        fi
        target="$(readlink -- "$link")"
        if [[ "$target" != /* ]]; then
            target="$(dirname -- "$link")/$target"
        fi
        target="$(realpath -m -- "$target")"
        # Only repository-owned links are ours to remove.
        if [[ "$target" != "$repo_dir"/* ]]; then
            continue
        fi
        if [[ -e "$link" ]]; then
            continue
        fi
        rm -- "$link"
        printf 'Removed stale link: %s -> %s\n' "$link" "$target"
    done
}

for source in "$repo_dir"/config/*; do
    link_file "$source" "$agent_dir/$(basename -- "$source")"
done

for command in pi-sandbox pi-sandbox-auth; do
    link_file "$repo_dir/bin/$command" "$agent_dir/bin/$command"
done

# Link each resource separately, preserving unrelated installed resources.
# Hidden placeholders are deliberately excluded by these globs.
for resource_dir in skills extensions prompts; do
    for source in "$repo_dir/$resource_dir"/*; do
        link_file "$source" "$agent_dir/$resource_dir/$(basename -- "$source")"
    done
done

for root in "$agent_dir" "$agent_dir/bin" "$agent_dir/skills" "$agent_dir/extensions" "$agent_dir/prompts"; do
    remove_stale_links "$root"
done

printf '\nSetup linked. From a project directory, run %q to start Pi in a local sandbox.\n' \
    "$agent_dir/bin/pi-sandbox"
if [[ -n "$backup_dir" ]]; then
    printf 'Previous files are saved in: %s\n' "$backup_dir"
fi
