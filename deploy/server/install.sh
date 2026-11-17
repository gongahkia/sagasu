#!/usr/bin/env bash
# Install or update Sagasu as a supervised service with a remote listener.
# Does not provision cloud resources, open cloud firewalls, or sign in to SMU.
set -euo pipefail

usage() {
    printf '%s\n' \
        'Usage:' \
        '  sudo bash deploy/server/install.sh --host NAME --key LABEL:SHA256HEX' \
        '  sudo bash deploy/server/install.sh --update' \
        '' \
        'Target: Ubuntu 24.04 on ARM64 or x86-64, run from the Sagasu source tree.' \
        '  --host    public host name agents will use (its DNS must point here)' \
        '  --key     label and SHA-256 hex digest of the first API key; the key' \
        '            itself never touches this machine' \
        '  --update  replace the code only; settings, keys and state are kept' \
        '' \
        'Installs Sagasu, Chromium, a virtual display, the sagasu and sagasu-xvfb' \
        'services, Caddy as the TLS front end, and opens local port 443.' \
        'Preserves an existing /etc/sagasu/sagasu.env and all /var/lib/sagasu state.'
}

HOST='' KEY='' UPDATE=0
while [[ $# -gt 0 ]]; do
    case $1 in
        --host) HOST=${2:-}; shift 2 ;;
        --key) KEY=${2:-}; shift 2 ;;
        --update) UPDATE=1; shift ;;
        --help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done
if [[ $EUID -ne 0 ]]; then
    printf '%s\n' 'Run this installer with sudo on the Ubuntu server.' >&2
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
if [[ $UPDATE -eq 1 ]]; then
    if [[ -n $HOST || -n $KEY || ! -e /etc/sagasu/sagasu.env ]]; then
        printf '%s\n' '--update takes no other option and needs an existing installation.' >&2
        exit 2
    fi
elif [[ ! -e /etc/sagasu/sagasu.env ]]; then
    if [[ ! $HOST =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ || ! $KEY =~ ^[a-z0-9][a-z0-9_-]{0,31}:[0-9a-f]{64}$ ]]; then
        usage >&2
        exit 2
    fi
fi

SOURCE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
KIT=$SOURCE/deploy/server
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a

apt-get update
apt-get install --yes python3 python3-venv ca-certificates rsync \
    xvfb xauth x11vnc novnc websockify caddy

if ! id sagasu >/dev/null 2>&1; then
    useradd --system --user-group --home-dir /var/lib/sagasu \
        --shell /usr/sbin/nologin --no-create-home sagasu
fi
install -d -o root -g root -m 0755 /opt/sagasu /opt/sagasu/bin /opt/sagasu/browsers
install -d -o sagasu -g sagasu -m 0700 /var/lib/sagasu
install -d -o root -g sagasu -m 0750 /etc/sagasu

systemctl stop sagasu.service 2>/dev/null || true

# Code, interpreter and browser are owned by root: the service cannot change them.
rsync -a --delete \
    --exclude .git --exclude .venv --exclude .env --exclude .sagasu \
    --exclude artifacts --exclude tests --exclude benchmarks --exclude asset \
    --exclude '__pycache__' --exclude '*.egg-info' --exclude build \
    "$SOURCE"/ /opt/sagasu/app/
chown -R root:root /opt/sagasu/app
[[ -x /opt/sagasu/venv/bin/python ]] || python3 -m venv /opt/sagasu/venv
/opt/sagasu/venv/bin/python -m pip install --quiet --upgrade pip
/opt/sagasu/venv/bin/python -m pip install --quiet --upgrade /opt/sagasu/app
/opt/sagasu/venv/bin/python -m pip install --quiet --force-reinstall --no-deps /opt/sagasu/app
/opt/sagasu/venv/bin/python -m playwright install-deps chromium
PLAYWRIGHT_BROWSERS_PATH=/opt/sagasu/browsers \
    /opt/sagasu/venv/bin/python -m playwright install chromium
chmod -R a+rX /opt/sagasu/browsers

for name in sagasu-launch sagasu-signin sagasu-ctl; do
    install -o root -g root -m 0755 "$KIT/$name" "/opt/sagasu/bin/$name"
done
ln -sf /opt/sagasu/bin/sagasu-signin /usr/local/bin/sagasu-signin
ln -sf /opt/sagasu/bin/sagasu-ctl /usr/local/bin/sagasu-ctl
install -o root -g root -m 0644 "$KIT/sagasu.service" /etc/systemd/system/sagasu.service
install -o root -g root -m 0644 "$KIT/sagasu-xvfb.service" /etc/systemd/system/sagasu-xvfb.service

if [[ ! -e /etc/sagasu/sagasu.env ]]; then
    sed -e "s/@PUBLIC_HOST@/$HOST/" -e "s/@KEY_DIGESTS@/$KEY/" \
        "$KIT/sagasu.env.example" >/etc/sagasu/sagasu.env.new
    chown root:sagasu /etc/sagasu/sagasu.env.new
    chmod 0640 /etc/sagasu/sagasu.env.new
    mv /etc/sagasu/sagasu.env.new /etc/sagasu/sagasu.env
fi

if [[ -n $HOST ]]; then
    if [[ -e /etc/caddy/Caddyfile && ! -e /etc/caddy/Caddyfile.before-sagasu ]]; then
        cp -p /etc/caddy/Caddyfile /etc/caddy/Caddyfile.before-sagasu
    fi
    sed -e "s/@PUBLIC_HOST@/$HOST/" "$KIT/Caddyfile.template" >/etc/caddy/Caddyfile
    caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null
fi

# Local firewall: allow HTTPS. Oracle's images keep their rules in this file;
# never use ufw there. The cloud provider's own firewall is a separate step.
ACCEPT=(-p tcp -m state --state NEW -m tcp --dport 443 -j ACCEPT)
if ! iptables -C INPUT "${ACCEPT[@]}" 2>/dev/null; then
    position=$(iptables -L INPUT --line-numbers -n | awk '$2 == "REJECT" || $2 == "DROP" {print $1; exit}')
    if [[ -n $position ]]; then
        iptables -I INPUT "$position" "${ACCEPT[@]}"
    fi
fi
RULES=/etc/iptables/rules.v4
if [[ -f $RULES ]] && ! grep -q -- '--dport 443 -j ACCEPT' "$RULES"; then
    cp -n "$RULES" "$RULES.before-sagasu"
    sed -i '0,/^-A INPUT -j REJECT/s//-A INPUT -p tcp -m state --state NEW -m tcp --dport 443 -j ACCEPT\n&/' "$RULES"
fi

systemctl daemon-reload
systemctl enable --now sagasu-xvfb.service sagasu.service
systemctl enable caddy.service
systemctl restart caddy.service

printf '%s\n' \
    '' \
    'Installed. Services:' \
    "  sagasu       $(systemctl is-active sagasu.service)" \
    "  sagasu-xvfb  $(systemctl is-active sagasu-xvfb.service)" \
    "  caddy        $(systemctl is-active caddy.service)" \
    '' \
    'Still to do, by a person:' \
    '  1. Allow inbound TCP 443 to this server in the cloud provider firewall.' \
    '  2. Sign in to SMU on this server:  sudo sagasu-signin' \
    'Then agents connect to https://HOST/mcp with header  Authorization: Bearer <key>.' \
    'Status: sudo sagasu-ctl status'
