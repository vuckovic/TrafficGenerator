#!/usr/bin/env bash
# One-time: push your SSH key to all nodes so Ansible can log in.
# Usage: ./bootstrap-ssh.sh [user]   (reads hosts from inventory.ini)
set -euo pipefail
USER_NAME="${1:-pi}"
[[ -f ~/.ssh/id_ed25519.pub ]] || ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519
grep -oP 'ansible_host=\K\S+' "$(dirname "$0")/inventory.ini" | while read -r h; do
  echo ">> $h"; ssh-copy-id -o StrictHostKeyChecking=accept-new "${USER_NAME}@${h}" </dev/tty
done
