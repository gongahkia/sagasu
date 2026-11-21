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
        '  --update  replace the code and the kit; keys, settings and state are' \
        '            kept, and a service you had disabled stays disabled' \
        '' \
        'Installs Sagasu, Chromium, a virtual display, the sagasu and sagasu-xvfb' \
        'services, Caddy as the TLS front end, and opens local port 443.' \
        'Never rewrites an existing /etc/sagasu/sagasu.env or /var/lib/sagasu state.'
}

HOST='' KEY='' UPDATE=0
while [[ $# -gt 0 ]]; do
    case $1 in
        --host) [[ $# -ge 2 ]] || { usage >&2; exit 2; }; HOST=$2; shift 2 ;;
        --key) [[ $# -ge 2 ]] || { usage >&2; exit 2; }; KEY=$2; shift 2 ;;
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

SETTINGS=/etc/sagasu/sagasu.env
HOST_RE='^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$'
KEY_RE='^[a-z0-9][a-z0-9_-]{0,31}:[0-9a-f]{64}$'
recorded_host() {
    sed -n 's/^SAGASU_HTTP_ALLOWED_HOSTS=\([a-z0-9.-]*\).*/\1/p' "$SETTINGS" | tail -n 1
}
if [[ -e $SETTINGS ]] && ! grep -qE '^SAGASU_HTTP_(PORT|SOCKET)=' "$SETTINGS"; then
    printf '%s\n' \
        "$SETTINGS exists but has no remote listener settings." \
        'It may belong to deploy/linux. Move it aside, or add the SAGASU_HTTP_*' \
        'settings from deploy/server/sagasu.env.example, then run this again.' >&2
    exit 2
fi
if [[ $UPDATE -eq 1 ]]; then
    if [[ -n $HOST || -n $KEY || ! -e $SETTINGS ]]; then
        printf '%s\n' '--update takes no other option and needs an existing installation.' >&2
        exit 2
    fi
elif [[ -e $SETTINGS ]]; then
    if [[ ( -n $HOST && $HOST != "$(recorded_host)" ) ||
        ( -n $KEY && $(grep -c -F -- "$KEY" "$SETTINGS") -eq 0 ) ]]; then
        printf '%s\n' \
            "This server already has $SETTINGS, so a different --host or --key would not be applied." \
            'New code: --update.  New key: sudo sagasu-ctl key-add LABEL.' \
            'New host name: change SAGASU_HTTP_ALLOWED_HOSTS in that file and the' \
            'site name in /etc/caddy/Caddyfile.' >&2
        exit 2
    fi
elif [[ ! $HOST =~ $HOST_RE || ${#HOST} -gt 253 || ! $KEY =~ $KEY_RE ]]; then
    usage >&2
    exit 2
fi

SOURCE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
KIT=$SOURCE/deploy/server
if [[ ! -f $SOURCE/pyproject.toml || ! -f $KIT/sagasu-launch ]]; then
    printf '%s\n' 'Run install.sh from inside the Sagasu source tree (deploy/server/install.sh).' >&2
    exit 2
fi
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a

# A service the owner switched off on purpose stays off.
LEAVE_STOPPED=0
if [[ -e /etc/systemd/system/sagasu.service ]] &&
    ! systemctl is-enabled --quiet sagasu.service; then
    LEAVE_STOPPED=1
fi
# The same for the gateway, once it exists.
LEAVE_GATEWAY_STOPPED=0
if [[ -e /etc/systemd/system/sagasu-gateway.service ]] &&
    ! systemctl is-enabled --quiet sagasu-gateway.service; then
    LEAVE_GATEWAY_STOPPED=1
fi

apt-get update
apt-get install --yes python3 python3-venv ca-certificates rsync curl \
    xvfb xauth x11vnc novnc websockify caddy

if ! id sagasu >/dev/null 2>&1; then
    useradd --system --user-group --home-dir /var/lib/sagasu \
        --shell /usr/sbin/nologin --no-create-home sagasu
fi
# The gateway (one address for every copy): its own account, no browser.
if ! id sagasu-gateway >/dev/null 2>&1; then
    useradd --system --user-group --home-dir /var/lib/sagasu-gateway \
        --shell /usr/sbin/nologin --no-create-home sagasu-gateway
fi
install -d -o sagasu-gateway -g sagasu-gateway -m 0700 /var/lib/sagasu-gateway
install -d -o root -g root -m 0755 /opt/sagasu /opt/sagasu/bin /opt/sagasu/browsers
install -d -o sagasu -g sagasu -m 0700 /var/lib/sagasu
# 0751: every copy's account must pass through to its own settings file, but
# none may list the folder; each file keeps its own owner and mode.
install -d -o root -g sagasu -m 0751 /etc/sagasu
# Teammates' copies (sagasu-ctl instance-add): their settings, web proxy sites
# and SSH keys, all owned by root.
install -d -o root -g root -m 0755 /etc/sagasu/instances /etc/caddy/sites /etc/ssh/sagasu_keys

# Code, interpreter and browser are owned by root: the service cannot change them.
rsync -a --delete \
    --exclude .git --exclude .venv --exclude .env --exclude .sagasu \
    --exclude artifacts --exclude tests --exclude benchmarks --exclude asset \
    --exclude '__pycache__' --exclude '*.egg-info' --exclude build \
    "$SOURCE"/ /opt/sagasu/app/
chown -R root:root /opt/sagasu/app
# Leftovers of an earlier build could otherwise be packaged in place of new code.
rm -rf /opt/sagasu/app/build /opt/sagasu/app/*.egg-info
[[ -x /opt/sagasu/venv/bin/python ]] || python3 -m venv /opt/sagasu/venv
/opt/sagasu/venv/bin/python -m pip install --quiet --upgrade pip

# From here the code the running service uses is replaced, so it is stopped.
# If a later step fails, it is started again with whatever is installed.
WAS_ACTIVE=0
if systemctl is-active --quiet sagasu.service; then
    WAS_ACTIVE=1
fi
# Teammates' copies run the same code, so they stop and start with it.
mapfile -t COPIES < <(systemctl list-units 'sagasu@*.service' --state=active --plain --no-legend | awk '{print $1}')
restore_service() {
    code=$?
    if [[ $code -ne 0 ]]; then
        printf '%s\n' '' 'The installer stopped part-way. Fix the problem shown above and run it again.' >&2
        if [[ $WAS_ACTIVE -eq 1 ]]; then
            printf '%s\n' 'Starting Sagasu again with what is installed.' >&2
            systemctl start sagasu.service || true
        fi
        for unit in "${COPIES[@]}"; do
            systemctl start "$unit" || true
        done
    fi
    exit "$code"
}
trap restore_service EXIT
systemctl stop sagasu.service "${COPIES[@]}" 2>/dev/null || true

/opt/sagasu/venv/bin/python -m pip install --quiet --upgrade /opt/sagasu/app
/opt/sagasu/venv/bin/python -m pip install --quiet --force-reinstall --no-deps /opt/sagasu/app
/opt/sagasu/venv/bin/python -m playwright install-deps chromium
PLAYWRIGHT_BROWSERS_PATH=/opt/sagasu/browsers \
    /opt/sagasu/venv/bin/python -m playwright install chromium
chmod -R a+rX /opt/sagasu/browsers

for name in sagasu-launch sagasu-signin sagasu-ctl sagasu-self; do
    install -o root -g root -m 0755 "$KIT/$name" "/opt/sagasu/bin/$name"
done
ln -sf /opt/sagasu/bin/sagasu-signin /usr/local/bin/sagasu-signin
ln -sf /opt/sagasu/bin/sagasu-ctl /usr/local/bin/sagasu-ctl
for unit in sagasu.service sagasu-xvfb.service sagasu@.service sagasu-xvfb@.service sagasu-gateway.service; do
    install -o root -g root -m 0644 "$KIT/$unit" "/etc/systemd/system/$unit"
done

# SSH for teammates: their keys live in a root-owned folder (never in a home
# folder their own processes could write), and nobody may open listening
# ports on this server through SSH. Checked before it takes effect.
SSHD_DROPIN=/etc/ssh/sshd_config.d/60-sagasu.conf
printf '%s\n' \
    '# Sagasu: teammates'"'"' SSH keys live in /etc/ssh/sagasu_keys, owned by root.' \
    '# Nobody may open listening ports on this server through SSH.' \
    'AuthorizedKeysFile .ssh/authorized_keys /etc/ssh/sagasu_keys/%u' \
    'AllowTcpForwarding local' >"$SSHD_DROPIN.new"
mv "$SSHD_DROPIN.new" "$SSHD_DROPIN"
if ! sshd -t; then
    rm -f "$SSHD_DROPIN"
    printf '%s\n' 'The SSH settings for teammates were refused by sshd and were not applied.' >&2
    exit 1
fi
if ! sshd -T 2>/dev/null | grep -qi '^authorizedkeysfile .*sagasu_keys'; then
    printf '%s\n' 'WARNING: another SSH setting overrides AuthorizedKeysFile; teammates cannot sign in until it is merged.' >&2
fi
systemctl reload ssh 2>/dev/null || true

if [[ ! -e $SETTINGS ]]; then
    sed -e "s/@PUBLIC_HOST@/$HOST/" -e "s/@KEY_DIGESTS@/$KEY/" \
        "$KIT/sagasu.env.example" >"$SETTINGS.new"
    chown root:sagasu "$SETTINGS.new"
    chmod 0640 "$SETTINGS.new"
    mv "$SETTINGS.new" "$SETTINGS"
fi
# Settings the template gained after this server was installed.
if ! grep -q '^SAGASU_AUTH_TIMEOUT_MS=' "$SETTINGS"; then
    printf '%s\n' 'SAGASU_AUTH_TIMEOUT_MS=900000' >>"$SETTINGS"
fi
# The web proxy now reaches Sagasu through a private socket file instead of a
# loopback port, which another local account could take while Sagasu is down.
# Done only when this kit also writes the proxy configuration below.
if grep -q '^SAGASU_HTTP_PORT=8765$' "$SETTINGS" &&
    ! grep -q '^SAGASU_HTTP_SOCKET=' "$SETTINGS" &&
    head -n 1 /etc/caddy/Caddyfile 2>/dev/null | grep -q '^# Publishes only Sagasu'; then
    sed -i 's|^SAGASU_HTTP_PORT=8765$|SAGASU_HTTP_SOCKET=/run/sagasu/http.sock|' "$SETTINGS"
fi

# Teammates' copies: refresh each proxy site sagasu-ctl wrote, so they publish
# the same paths as the owner's (checked with the whole configuration below).
for instance in /etc/sagasu/instances/*; do
    name=${instance##*/}
    site=/etc/caddy/sites/$name.caddy
    [[ -f $instance && $name =~ ^[a-z][a-z0-9]{1,15}$ ]] || continue
    head -n 1 "$site" 2>/dev/null | grep -q "^# One teammate's copy of Sagasu" || continue
    site_host=$(sed -n 's/^HOST=\([a-z0-9.-]*\)$/\1/p' "$instance")
    [[ -n $site_host ]] || continue
    sed -e "s/@HOST@/$site_host/" -e "s#@SOCKET@#/run/sagasu-$name/http.sock#" \
        "$KIT/Caddysite.template" >"$site.sagasu-new"
    chmod 0644 "$site.sagasu-new"
    mv "$site.sagasu-new" "$site"
done

# Saved SMU sign-ins (sagasu/saved_sign_in.py): each copy gets its own key,
# readable only by that copy's account. Created once and never rewritten: a new
# key would make every sign-in saved with the old one unreadable.
ensure_signin_key() {
    local name=$1 account=$2 env_file=$3
    local key=/etc/sagasu/$name.signin-key
    if [[ ! -s $key ]]; then
        (umask 077 && { head -c 32 /dev/urandom | base64 -w0; echo; } >"$key.new")
        chown "root:$account" "$key.new"
        chmod 0640 "$key.new"
        mv "$key.new" "$key"
        # Switched on only when this run made the key. A key without the
        # line means the copy was switched off on purpose; leave it off.
        if ! grep -q '^SAGASU_HTTP_SIGN_IN_KEY_FILE=' "$env_file"; then
            printf 'SAGASU_HTTP_SIGN_IN_KEY_FILE=%s\n' "$key" >>"$env_file"
        fi
    else
        chown "root:$account" "$key"
        chmod 0640 "$key"
    fi
}
ensure_signin_key sagasu sagasu "$SETTINGS"
for instance in /etc/sagasu/instances/*; do
    name=${instance##*/}
    [[ -f $instance && $name =~ ^[a-z][a-z0-9]{1,15}$ ]] || continue
    [[ -f /etc/sagasu/$name.env ]] && id "sagasu-$name" >/dev/null 2>&1 || continue
    ensure_signin_key "$name" "sagasu-$name" "/etc/sagasu/$name.env"
done

# The gateway: one address (the owner's host name) for every copy.
GATEWAY_SETTINGS=/etc/sagasu/gateway.env
if [[ ! -e $GATEWAY_SETTINGS ]]; then
    # App sign-in at the shared address follows the owner's own setting (off
    # when it was never set, as for a copy).
    oauth=$(sed -n 's/^SAGASU_HTTP_OAUTH=\(on\|off\)$/\1/p' "$SETTINGS" | tail -n 1)
    redirects=$(sed -n 's/^SAGASU_OAUTH_REDIRECT_HOSTS=\([a-z0-9.,-]*\)$/\1/p' "$SETTINGS" | tail -n 1)
    {
        printf '%s\n' \
            '# The gateway: one address for every copy (sagasu/gateway.py).' \
            "SAGASU_GATEWAY_HOST=$(recorded_host)" \
            'SAGASU_GATEWAY_SOCKET=/run/sagasu-gateway/http.sock' \
            'SAGASU_GATEWAY_ROUTES=/etc/sagasu/gateway-routes.json' \
            'SAGASU_GATEWAY_STATE=/var/lib/sagasu-gateway' \
            "SAGASU_GATEWAY_OAUTH=${oauth:-off}"
        [[ -z $redirects ]] || printf 'SAGASU_OAUTH_REDIRECT_HOSTS=%s\n' "$redirects"
    } >"$GATEWAY_SETTINGS.new"
    chown root:sagasu-gateway "$GATEWAY_SETTINGS.new"
    chmod 0640 "$GATEWAY_SETTINGS.new"
    mv "$GATEWAY_SETTINGS.new" "$GATEWAY_SETTINGS"
fi
# Its routing table, while the copies are stopped: any copy given its gateway
# key now starts with it, without another restart. Fails (and so does this
# install) if the owner's copy cannot be put on the shared address.
/opt/sagasu/bin/sagasu-ctl gateway-sync --no-restart --strict

# An update refreshes a proxy configuration this kit wrote, never one written by hand.
if [[ -z $HOST ]] && head -n 1 /etc/caddy/Caddyfile 2>/dev/null |
    grep -q '^# Publishes only Sagasu'; then
    HOST=$(recorded_host)
fi
if [[ -n $HOST ]]; then
    if [[ -e /etc/caddy/Caddyfile && ! -e /etc/caddy/Caddyfile.before-sagasu ]]; then
        cp -p /etc/caddy/Caddyfile /etc/caddy/Caddyfile.before-sagasu
    fi
    sed -e "s/@PUBLIC_HOST@/$HOST/" "$KIT/Caddyfile.template" >/etc/caddy/Caddyfile.sagasu-new
    caddy validate --config /etc/caddy/Caddyfile.sagasu-new --adapter caddyfile >/dev/null
    # Kept, so a gateway that fails its first start can be backed out below.
    [[ ! -e /etc/caddy/Caddyfile ]] || cp -p /etc/caddy/Caddyfile /etc/caddy/Caddyfile.previous
    mv /etc/caddy/Caddyfile.sagasu-new /etc/caddy/Caddyfile
fi

# Local firewall: allow HTTPS. Oracle's images keep their rules in this file;
# never use ufw there. The cloud provider's own firewall is a separate step.
ACCEPT=(-p tcp -m state --state NEW -m tcp --dport 443 -j ACCEPT)
if ! iptables -C INPUT "${ACCEPT[@]}" 2>/dev/null; then
    position=$(iptables -L INPUT --line-numbers -n |
        awk '$2 == "REJECT" || $2 == "DROP" {print $1; exit}')
    if [[ -n $position ]]; then
        iptables -I INPUT "$position" "${ACCEPT[@]}"
    elif [[ $(iptables -S INPUT) != '-P INPUT ACCEPT'* ]]; then
        printf '%s\n' 'WARNING: no REJECT or DROP rule was found in INPUT to place the port 443 rule before. Open TCP 443 in this firewall yourself.' >&2
    fi
fi
RULES=/etc/iptables/rules.v4
SAVED='^-A INPUT -p tcp .*--dport 443 -j ACCEPT'
if [[ -f $RULES ]] && ! grep -q -- "$SAVED" "$RULES"; then
    cp -n "$RULES" "$RULES.before-sagasu"
    sed -i '0,/^-A INPUT -j \(REJECT\|DROP\)/s//-A INPUT -p tcp -m state --state NEW -m tcp --dport 443 -j ACCEPT\n&/' "$RULES"
fi
if [[ ! -f $RULES ]] || ! grep -q -- "$SAVED" "$RULES"; then
    printf '%s\n' "WARNING: the port 443 rule is not saved in $RULES, so it will be gone after a reboot." >&2
fi

systemctl daemon-reload

# The gateway, before any copy starts again. The first time, the owner's
# copy's OAuth sign-ins move to it while that copy is stopped, so apps signed
# in at this address stay signed in (and nothing keeps using the old file).
MOVED_STORE=0
if [[ ! -e /var/lib/sagasu-gateway/oauth.sqlite3 && -f /var/lib/sagasu/oauth.sqlite3 ]]; then
    install -o sagasu-gateway -g sagasu-gateway -m 0600 \
        /var/lib/sagasu/oauth.sqlite3 /var/lib/sagasu-gateway/oauth.sqlite3
    mv /var/lib/sagasu/oauth.sqlite3 /var/lib/sagasu/oauth.sqlite3.moved-to-gateway
    MOVED_STORE=1
fi
gateway_answers() {
    # A request without a key must get the gateway's own 401.
    local host code
    host=$(sed -n 's/^SAGASU_GATEWAY_HOST=\([a-z0-9.-]*\)$/\1/p' "$GATEWAY_SETTINGS")
    for _ in $(seq 1 30); do
        code=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
            --unix-socket /run/sagasu-gateway/http.sock -H "Host: $host" \
            http://localhost/mcp 2>/dev/null || true)
        [[ $code == 401 ]] && return 0
        sleep 1
    done
    return 1
}
if [[ $LEAVE_GATEWAY_STOPPED -eq 0 ]]; then
    systemctl enable sagasu-gateway.service
    # A failed start is handled below (backed out), not by set -e.
    systemctl restart sagasu-gateway.service || true
    if ! gateway_answers; then
        printf '%s\n' '' \
            'The gateway did not start; backing it out. The owner'"'"'s address goes on' \
            'serving the owner'"'"'s copy directly. See: journalctl -u sagasu-gateway' >&2
        systemctl disable --now sagasu-gateway.service || true
        if [[ $MOVED_STORE -eq 1 ]]; then
            mv /var/lib/sagasu/oauth.sqlite3.moved-to-gateway /var/lib/sagasu/oauth.sqlite3
            rm -f /var/lib/sagasu-gateway/oauth.sqlite3
        fi
        [[ ! -e /etc/caddy/Caddyfile.previous ]] ||
            cp -p /etc/caddy/Caddyfile.previous /etc/caddy/Caddyfile
        exit 1
    fi
fi

# The action history now keeps who started each write (the person at the
# server, or an agent through the remote listener), and a remote caller sees
# only agents' writes. Earlier writes have no reliable origin: the local MCP
# can also write on a hosted copy. Keep unknown history private to the
# host, with a backup, rather than granting remote access by assumption.
mark_history_local() {
    local user=$1 db=$2
    [[ -f $db ]] || return 0
    [[ -e $db.before-origins ]] || cp -p "$db" "$db.before-origins" || true
    runuser -u "$user" -- /opt/sagasu/venv/bin/python -c \
        'import sys; from sagasu.action_journal import ActionJournal; n = ActionJournal(sys.argv[1]).mark_unrecorded("local"); n and print(f"{sys.argv[1]}: {n} earlier write(s) kept private to the host")' \
        "$db" || true
}
mark_history_local sagasu /var/lib/sagasu/actions.sqlite3
for instance in /etc/sagasu/instances/*; do
    copy=${instance##*/}
    [[ -f $instance && $copy =~ ^[a-z][a-z0-9]{1,15}$ ]] || continue
    mark_history_local "sagasu-$copy" "/var/lib/sagasu-$copy/actions.sqlite3"
done

# Teammates' copies come back whatever the owner chose for their own.
for unit in "${COPIES[@]}"; do
    systemctl restart "sagasu-xvfb@${unit#sagasu@}" || true
    systemctl start "$unit" || true
done
if [[ $LEAVE_STOPPED -eq 1 ]]; then
    systemctl reload-or-restart caddy.service || true
    printf '%s\n' '' \
        'Sagasu was disabled before this run. It has been updated and left stopped.' \
        'To bring it back: sudo systemctl enable --now sagasu-xvfb sagasu caddy'
    exit 0
fi
systemctl enable sagasu-xvfb.service sagasu.service caddy.service
# Sagasu is stopped here, so the display can take a changed unit file.
systemctl restart sagasu-xvfb.service
systemctl start sagasu.service
systemctl reload-or-restart caddy.service

printf '%s\n' \
    '' \
    'Installed. Services:' \
    "  sagasu       $(systemctl is-active sagasu.service)" \
    "  sagasu-xvfb  $(systemctl is-active sagasu-xvfb.service)" \
    "  caddy        $(systemctl is-active caddy.service)" \
    "  gateway      $(systemctl is-active sagasu-gateway.service)" \
    '' \
    'Still to do, by a person, on a first install:' \
    '  1. Allow inbound TCP 443 to this server in the cloud provider firewall.' \
    '  2. Sign in to SMU on this server:  sudo sagasu-signin' \
    'Then agents connect to https://HOST/mcp with header  Authorization: Bearer <key>.' \
    'Status: sudo sagasu-ctl status     Teammates'"'"' copies: sudo sagasu-ctl instance-list'
