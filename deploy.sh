#!/usr/bin/env bash
# LocalDNS deploy script — idempotent, safe to re-run.
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }
die()   { error "$*"; exit 1; }
step()  { echo -e "\n${CYAN}▶ $*${NC}"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo -e "\n${CYAN}╔══════════════════════════════════════╗"
echo -e "║        LocalDNS — Deploy Script      ║"
echo -e "╚══════════════════════════════════════╝${NC}\n"

# ── Phase 1: Preflight ──────────────────────────────────────────────────────

step "Phase 1: Preflight checks"

command -v docker &>/dev/null || die "docker not found. Install Docker: https://docs.docker.com/engine/install/"
docker compose version &>/dev/null || die "'docker compose' (v2) not found. Upgrade Docker to ≥ 23."

if [[ $EUID -eq 0 ]]; then
    warn "Running as root. Consider using a non-root user with docker group access."
elif ! groups 2>/dev/null | grep -qw docker; then
    die "User '$USER' is not in the 'docker' group.\nRun: sudo usermod -aG docker $USER\nThen log out and back in (or run: newgrp docker)"
fi

# Check port 53
if ss -tulnp 2>/dev/null | grep -q ':53 '; then
    if ss -tulnp 2>/dev/null | grep ':53 ' | grep -qE 'systemd-resolve|resolved|stub'; then
        error "Port 53 is occupied by systemd-resolved stub listener."
        echo ""
        echo "  Fix it by running these commands, then re-run deploy.sh:"
        echo ""
        echo "    sudo sed -i '/^#DNSStubListener/s/.*/DNSStubListener=no/' /etc/systemd/resolved.conf"
        echo "    grep -q '^DNSStubListener' /etc/systemd/resolved.conf || echo 'DNSStubListener=no' | sudo tee -a /etc/systemd/resolved.conf"
        echo "    sudo systemctl restart systemd-resolved"
        echo ""
        exit 1
    else
        warn "Port 53 is already in use by another process."
        read -rp "Continue anyway? [y/N] " _reply
        [[ "$_reply" =~ ^[Yy]$ ]] || exit 1
    fi
fi

mkdir -p "$SCRIPT_DIR/data"
info "data/ directory ready."

# ── Phase 2: Environment ────────────────────────────────────────────────────

step "Phase 2: Environment setup"

if [[ ! -f "$SCRIPT_DIR/.env" ]]; then
    cp "$SCRIPT_DIR/.env.example" "$SCRIPT_DIR/.env"
    # Generate a 64-char random secret key (alphanumeric + safe symbols)
    _SECRET=$(LC_ALL=C tr -dc 'A-Za-z0-9!@#%^&*_+=-' </dev/urandom 2>/dev/null | head -c 64 || true)
    if [[ ${#_SECRET} -lt 32 ]]; then
        _SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(48))")
    fi
    sed -i "s|^SECRET_KEY=.*|SECRET_KEY=${_SECRET}|" "$SCRIPT_DIR/.env"
    info ".env created with a generated SECRET_KEY."
    unset _SECRET
else
    info ".env already exists — skipping generation."
fi

# Load .env variables into shell
set -a
# shellcheck source=.env
source "$SCRIPT_DIR/.env"
set +a

# ── Phase 3: Admin account (first run only) ─────────────────────────────────

step "Phase 3: Admin account"

_FIRST_RUN=false
_ADMIN_USER=""
_ADMIN_PASS=""

if [[ ! -f "$SCRIPT_DIR/data/dns.db" ]]; then
    _FIRST_RUN=true
    info "No database found — first run."
elif command -v sqlite3 &>/dev/null; then
    _USER_COUNT=$(sqlite3 "$SCRIPT_DIR/data/dns.db" "SELECT COUNT(*) FROM users;" 2>/dev/null || echo "0")
    if [[ "$_USER_COUNT" == "0" ]]; then
        _FIRST_RUN=true
        info "Database exists but has no users — creating admin."
    else
        info "Admin account already exists — skipping."
    fi
else
    warn "sqlite3 not installed, cannot verify users table. Assuming first run."
    _FIRST_RUN=true
fi

if [[ "$_FIRST_RUN" == "true" ]]; then
    read -rp "  Admin username [admin]: " _ADMIN_USER
    _ADMIN_USER="${_ADMIN_USER:-admin}"

    while true; do
        read -rsp "  Admin password (min 10 chars): " _ADMIN_PASS
        echo
        if [[ ${#_ADMIN_PASS} -lt 10 ]]; then
            error "Password must be at least 10 characters."
            continue
        fi
        read -rsp "  Confirm password: " _ADMIN_PASS2
        echo
        if [[ "$_ADMIN_PASS" == "$_ADMIN_PASS2" ]]; then
            unset _ADMIN_PASS2
            break
        fi
        error "Passwords do not match. Try again."
    done
    info "Credentials collected."
fi

# ── Phase 4: Build & start ──────────────────────────────────────────────────

step "Phase 4: Build Docker images"

if docker image inspect localdns-dns &>/dev/null && docker image inspect localdns-webui &>/dev/null; then
    info "Images exist — building with cache."
    docker compose build
else
    info "First build — no cache."
    docker compose build --no-cache
fi

step "Starting services"
docker compose up -d

# Create admin user after containers start (webui initialises the DB on startup)
if [[ "$_FIRST_RUN" == "true" && -n "$_ADMIN_USER" ]]; then
    info "Waiting for webui to initialise database (5s)..."
    sleep 5
    echo "$_ADMIN_PASS" | docker compose run --rm -T webui python3 init_admin.py "$_ADMIN_USER"
    _ADMIN_PASS=""
    info "Admin user '$_ADMIN_USER' created."
fi

# Poll for DNS container health
info "Waiting for DNS server to be ready (up to 15s)..."
for _i in $(seq 1 15); do
    if docker compose ps dns 2>/dev/null | grep -qE 'Up|running'; then
        info "DNS server is running."
        break
    fi
    sleep 1
    if [[ $_i -eq 15 ]]; then
        warn "DNS server did not report healthy. Check: docker compose logs dns"
    fi
done

# ── Phase 5: Systemd service ────────────────────────────────────────────────

step "Phase 5: Systemd service"

_UNIT_SRC="$SCRIPT_DIR/systemd/localdns.service"
_UNIT_DEST="/etc/systemd/system/localdns.service"
_CURRENT_USER="${SUDO_USER:-$USER}"

# Fill in placeholders
sed -e "s|%%PROJECT_DIR%%|$SCRIPT_DIR|g" \
    -e "s|%%USER%%|$_CURRENT_USER|g" \
    "$_UNIT_SRC" > /tmp/localdns.service.generated

_NEEDS_INSTALL=true
if [[ -f "$_UNIT_DEST" ]]; then
    _SRC_MD5=$(md5sum /tmp/localdns.service.generated | awk '{print $1}')
    _DST_MD5=$(md5sum "$_UNIT_DEST" 2>/dev/null | awk '{print $1}')
    if [[ "$_SRC_MD5" == "$_DST_MD5" ]]; then
        _NEEDS_INSTALL=false
        info "Systemd unit is already up to date."
    fi
fi

if [[ "$_NEEDS_INSTALL" == "true" ]]; then
    if sudo cp /tmp/localdns.service.generated "$_UNIT_DEST" 2>/dev/null; then
        sudo systemctl daemon-reload
        sudo systemctl enable localdns.service
        if systemctl is-active --quiet localdns.service 2>/dev/null; then
            sudo systemctl restart localdns.service
        else
            sudo systemctl start localdns.service
        fi
        info "Systemd service installed and enabled."
    else
        warn "Could not install systemd service (sudo required). Install manually:"
        echo "  sudo cp /tmp/localdns.service.generated $_UNIT_DEST"
        echo "  sudo systemctl daemon-reload && sudo systemctl enable --now localdns.service"
    fi
fi

rm -f /tmp/localdns.service.generated

# ── Summary ─────────────────────────────────────────────────────────────────

_HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "your-server-ip")
_PORT="${WEBUI_PORT:-5380}"
_HOST="${WEBUI_HOST:-127.0.0.1}"

echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════╗"
echo -e "║           LocalDNS deployed successfully!            ║"
echo -e "╚══════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${CYAN}DNS server${NC}  : ${_HOST_IP}:53  (UDP + TCP)"
echo -e "  ${CYAN}Web UI${NC}      : http://${_HOST}:${_PORT}"
echo ""
echo "  How to use this DNS server:"
echo ""
echo "    Linux (this machine):"
echo "      Add 'nameserver 127.0.0.1' to /etc/resolv.conf"
echo "      OR configure systemd-resolved to forward to ${_HOST_IP}"
echo ""
echo "    Router / DHCP (recommended for whole network):"
echo "      Set primary DNS to ${_HOST_IP}"
echo ""
echo "    Windows client:"
echo "      Preferred DNS: ${_HOST_IP}"
echo ""
echo "  Useful commands:"
echo "    docker compose logs -f            # Live logs"
echo "    docker compose restart dns        # Restart DNS"
echo "    dig @127.0.0.1 example.home A     # Test local record"
echo "    dig @127.0.0.1 google.com A       # Test upstream failover"
echo ""
