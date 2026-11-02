#!/usr/bin/env bash
# Install into an existing Ubuntu VM. Does not provision cloud resources or start polling.
set -euo pipefail

if [[ ${1:-} == --help ]]; then
    printf '%s\n' \
        'Usage: sudo bash deploy/linux/install.sh' \
        'Target: Ubuntu 24.04 on ARM64 or x86-64.' \
        'Installs Sagasu, Chromium, sagasu-host and a stopped systemd service.' \
        'Preserves /etc/sagasu/sagasu.env and all /var/lib/sagasu state.'
    exit 0
fi
if [[ $# -ne 0 ]]; then
    printf '%s\n' 'Unexpected arguments. Use --help for usage.' >&2
    exit 2
fi
if [[ $EUID -ne 0 ]]; then
    printf '%s\n' 'Run this installer with sudo on the Ubuntu VM.' >&2
    exit 2
fi

# shellcheck disable=SC1091
source /etc/os-release
if [[ $ID != ubuntu || $VERSION_ID != 24.04 ]]; then
    printf '%s\n' 'This installer targets Ubuntu 24.04. No changes were made.' >&2
    exit 2
fi
case $(uname -m) in
    aarch64|x86_64) ;;
    *) printf '%s\n' 'Unsupported CPU architecture.' >&2; exit 2 ;;
esac
command -v systemctl >/dev/null
if systemctl is-active --quiet sagasu.service; then
    printf '%s\n' 'Stop Sagasu before updating: sudo systemctl stop sagasu' >&2
    exit 2
fi

SAGASU_INSTALL_SOURCE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install --yes python3 python3-venv ca-certificates

if ! id sagasu >/dev/null 2>&1; then
    useradd --system --user-group --home-dir /var/lib/sagasu \
        --shell /usr/sbin/nologin --no-create-home sagasu
fi
install -d -o root -g root -m 0755 /opt/sagasu
install -d -o sagasu -g sagasu -m 0700 /var/lib/sagasu
install -d -o root -g sagasu -m 0750 /etc/sagasu
python3 -m venv /opt/sagasu/venv
/opt/sagasu/venv/bin/python -m pip install --upgrade "$SAGASU_INSTALL_SOURCE"
/opt/sagasu/venv/bin/python -m playwright install-deps chromium
runuser -u sagasu -- env PLAYWRIGHT_BROWSERS_PATH=/var/lib/sagasu/browsers \
    /opt/sagasu/venv/bin/python -m playwright install chromium

if [[ ! -e /etc/sagasu/sagasu.env ]]; then
    install -o root -g sagasu -m 0640 \
        "$SAGASU_INSTALL_SOURCE/deploy/linux/sagasu.env.example" /etc/sagasu/sagasu.env
fi
install -o root -g root -m 0755 \
    "$SAGASU_INSTALL_SOURCE/deploy/linux/sagasu-host" /usr/local/bin/sagasu-host
install -o root -g root -m 0644 \
    "$SAGASU_INSTALL_SOURCE/deploy/linux/sagasu.service" /etc/systemd/system/sagasu.service
systemctl daemon-reload
printf '%s\n' \
    'Installed. The service has not been started or enabled.' \
    'Next: sudoedit /etc/sagasu/sagasu.env' \
    'Then: sudo sagasu-host doctor --timeout 30' \
    'Stop any other bot using this token before starting the VM service.' \
    'Start: sudo systemctl enable --now sagasu'
