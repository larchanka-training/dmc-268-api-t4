#!/usr/bin/env bash
# One-time, idempotent setup of the team 4 VPS: Docker Engine, the deploy user, app directories.
# Run as root. DEPLOY_PUBKEY must hold the contents of ops/deploy_key.pub.
# Usage: ssh root@<host> "DEPLOY_PUBKEY='<key>' bash -s" < ops/bootstrap.sh
set -euo pipefail

readonly DEPLOY_USER=deploy
readonly APP_DIR=/opt/dmc268
readonly DOCKER_KEYRING=/etc/apt/keyrings/docker.asc
readonly DOCKER_SOURCE=/etc/apt/sources.list.d/docker.list
readonly DOCKER_PACKAGES=(docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin)
readonly APT=(apt-get -o DPkg::Lock::Timeout=300 -y -qq)

fail() {
  echo "bootstrap: $*" >&2
  exit 1
}

check_preconditions() {
  [ "$(id -u)" -eq 0 ] || fail "must run as root"
  [ -n "${DEPLOY_PUBKEY:-}" ] || fail "DEPLOY_PUBKEY is empty"
  [[ "$DEPLOY_PUBKEY" =~ ^ssh-ed25519\ [A-Za-z0-9+/]+=*(\ [A-Za-z0-9._@-]+)?$ ]] \
    || fail "DEPLOY_PUBKEY is not a single ssh-ed25519 public key"
}

docker_installed() {
  local status
  status=$(dpkg-query -W -f='${Status}\n' "${DOCKER_PACKAGES[@]}" 2>/dev/null) || return 1
  ! grep -qv '^install ok installed$' <<<"$status"
}

install_docker() {
  if docker_installed; then
    echo "docker packages already installed"
    return
  fi
  export DEBIAN_FRONTEND=noninteractive

  "${APT[@]}" update
  "${APT[@]}" install ca-certificates curl
  install -m 0755 -d /etc/apt/keyrings
  if [ ! -s "$DOCKER_KEYRING" ]; then
    curl -fsSL https://download.docker.com/linux/debian/gpg -o "$DOCKER_KEYRING"
  fi
  chmod a+r "$DOCKER_KEYRING"

  local codename arch source_line
  codename=$(. /etc/os-release && echo "$VERSION_CODENAME")
  arch=$(dpkg --print-architecture)
  source_line="deb [arch=$arch signed-by=$DOCKER_KEYRING] https://download.docker.com/linux/debian $codename stable"
  if [ "$(cat "$DOCKER_SOURCE" 2>/dev/null)" != "$source_line" ]; then
    echo "$source_line" > "$DOCKER_SOURCE"
  fi

  "${APT[@]}" update
  "${APT[@]}" install "${DOCKER_PACKAGES[@]}"
}

setup_deploy_user() {
  if ! id "$DEPLOY_USER" >/dev/null 2>&1; then
    useradd --create-home --shell /bin/bash "$DEPLOY_USER"
  fi
  # Key-only login: '*' means no valid password, unlike '!' which locks the account for sshd.
  usermod --password '*' "$DEPLOY_USER"
  usermod --append --groups docker "$DEPLOY_USER"

  local home ssh_dir
  home=$(getent passwd "$DEPLOY_USER" | cut -d: -f6)
  ssh_dir="$home/.ssh"
  install -d -m 700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$ssh_dir"
  printf '%s\n' "$DEPLOY_PUBKEY" > "$ssh_dir/authorized_keys"
  chown "$DEPLOY_USER:$DEPLOY_USER" "$ssh_dir/authorized_keys"
  chmod 600 "$ssh_dir/authorized_keys"
}

create_app_dirs() {
  install -d -m 755 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$APP_DIR" "$APP_DIR/frontend"
}

print_summary() {
  echo "--- bootstrap result"
  docker --version
  docker compose version
  echo "docker service: $(systemctl is-active docker)"
  id "$DEPLOY_USER"
  stat -c '%A %U:%G %n' "$APP_DIR" "$APP_DIR/frontend"
}

main() {
  check_preconditions
  install_docker
  systemctl enable --now docker
  setup_deploy_user
  create_app_dirs
  print_summary
}

main
