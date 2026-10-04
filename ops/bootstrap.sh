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
# rsync must exist on both ends: the frontend deploy copies dist/ with it.
readonly EXTRA_PACKAGES=(rsync)
readonly APT=(apt-get -o DPkg::Lock::Timeout=300 -y -qq)
readonly RESOLVED_DROPIN=/etc/systemd/resolved.conf.d/90-dmc268-no-multicast.conf

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

install_extra_packages() {
  local missing=() package
  for package in "${EXTRA_PACKAGES[@]}"; do
    if ! dpkg-query -W -f='${Status}\n' "$package" 2>/dev/null | grep -qx 'install ok installed'; then
      missing+=("$package")
    fi
  done
  if [ "${#missing[@]}" -eq 0 ]; then
    echo "extra packages already installed: ${EXTRA_PACKAGES[*]}"
    return
  fi
  export DEBIAN_FRONTEND=noninteractive
  "${APT[@]}" update
  "${APT[@]}" install "${missing[@]}"
  echo "installed: ${missing[*]}"
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

# LLMNR (5355) and mDNS (5353) resolve names on a local network; on an internet-facing
# server they only widen the attack surface.
disable_multicast_name_resolution() {
  if ! systemctl cat systemd-resolved.service >/dev/null 2>&1; then
    echo "systemd-resolved not installed, nothing to disable"
    return
  fi
  local wanted
  wanted=$'[Resolve]\nLLMNR=no\nMulticastDNS=no'
  if [ "$(cat "$RESOLVED_DROPIN" 2>/dev/null)" != "$wanted" ]; then
    install -d -m 755 "$(dirname "$RESOLVED_DROPIN")"
    printf '%s\n' "$wanted" > "$RESOLVED_DROPIN"
    systemctl try-restart systemd-resolved
    echo "LLMNR and mDNS disabled"
  else
    echo "LLMNR and mDNS already disabled"
  fi
}

create_app_dirs() {
  install -d -m 755 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$APP_DIR" "$APP_DIR/frontend"
}

print_summary() {
  echo "--- bootstrap result"
  docker --version
  docker compose version
  echo "docker service: $(systemctl is-active docker)"
  echo "rsync: $(rsync --version 2>/dev/null | awk 'NR == 1' || echo n/a)"
  id "$DEPLOY_USER"
  stat -c '%A %U:%G %n' "$APP_DIR" "$APP_DIR/frontend"
  local protocols
  # awk reads all of resolvectl's output: an early exit would SIGPIPE it and trip pipefail.
  protocols=$(resolvectl status 2>/dev/null | awk '/^ *Protocols:/ && !seen { sub(/^ +/, ""); print; seen = 1 }')
  echo "resolved: ${protocols:-n/a}"
}

main() {
  check_preconditions
  install_docker
  systemctl enable --now docker
  install_extra_packages
  setup_deploy_user
  create_app_dirs
  disable_multicast_name_resolution
  print_summary
}

main
