#!/usr/bin/env bash
#
# install.sh - deploy the Plannotator plan hub on a server.
#
# Copies the hub program and page files into a prefix, installs the hub
# systemd unit, and prints the remaining manual steps. Idempotent: re-running
# it simply overwrites the files it manages.
#
# Paths this script writes:
#   <prefix>/                program, pages, env example, workstation assets
#   /etc/systemd/system/     plan-hub.service only
# Nothing else is touched. The per-workstation units are staged under
# <prefix>/systemd/ for you to deploy to each workstation yourself.
#
# Usage:
#   ./install.sh [--prefix DIR] [--dry-run] [--help]

set -euo pipefail

PREFIX="/opt/plan-hub"
SYSTEMD_DIR="/etc/systemd/system"
DRY_RUN=0

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<'EOF'
Plannotator plan hub installer

Usage:
  ./install.sh [options]

Options:
  --prefix DIR   Install prefix (default: /opt/plan-hub)
  --dry-run      Print what would happen, change nothing
  -h, --help     Show this help and exit

Files written:
  <prefix>/plan-hub.py            hub daemon
  <prefix>/plans-page.html        plan archive browser
  <prefix>/sessions-page.html     live sessions page (if present in repo)
  <prefix>/plan-hub.env.example   example environment file
  <prefix>/workstation/           per-workstation update script
  <prefix>/systemd/               workstation units (deploy yourself)
  /etc/systemd/system/plan-hub.service

Next steps are printed when the installer finishes.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --prefix)
      PREFIX="${2:?--prefix needs a directory}"
      shift 2
      ;;
    --prefix=*)
      PREFIX="${1#--prefix=}"
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

say() { printf '%s\n' "$*"; }
run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '  [dry-run] %s\n' "$*"
  else
    printf '  + %s\n' "$*"
    "$@"
  fi
}

say "Plannotator plan hub installer"
say "  source:  $SRC_DIR"
say "  prefix:  $PREFIX"
say "  unitdir: $SYSTEMD_DIR"
if [ "$DRY_RUN" -eq 1 ]; then
  say "  mode:    dry-run (no changes)"
fi

# --- 1. install prefix -----------------------------------------------------
say ""
say "==> Creating install prefix"
run mkdir -p "$PREFIX" "$PREFIX/systemd" "$PREFIX/workstation"

# --- 2. hub program + page files ------------------------------------------
say ""
say "==> Installing hub program and pages"
run install -m 0644 "$SRC_DIR/hub/plan-hub.py" "$PREFIX/plan-hub.py"
for page in plans-page.html sessions-page.html; do
  if [ -f "$SRC_DIR/hub/$page" ]; then
    run install -m 0644 "$SRC_DIR/hub/$page" "$PREFIX/$page"
  else
    say "  (skip $page - not in repo; the hub uses its built-in page)"
  fi
done

# --- 3. env example + workstation assets ----------------------------------
say ""
say "==> Staging configuration and workstation assets"
run install -m 0644 "$SRC_DIR/systemd/plan-hub.env.example" "$PREFIX/plan-hub.env.example"
run install -m 0755 "$SRC_DIR/workstation/plannotator-update.sh" "$PREFIX/workstation/plannotator-update.sh"
for unit in "$SRC_DIR"/systemd/plannotator-*.service "$SRC_DIR"/systemd/plannotator-*.timer; do
  [ -e "$unit" ] || continue
  run install -m 0644 "$unit" "$PREFIX/systemd/$(basename "$unit")"
done

# --- 4. hub systemd unit ---------------------------------------------------
say ""
say "==> Installing hub systemd unit"
if [ "$(id -u)" -eq 0 ] || [ "$DRY_RUN" -eq 1 ]; then
  run install -m 0644 "$SRC_DIR/systemd/plan-hub.service" "$SYSTEMD_DIR/plan-hub.service"
else
  say "  ! not root - skipping $SYSTEMD_DIR/plan-hub.service"
  say "    re-run the systemd step with sudo to install the hub unit"
fi

# --- 5. next steps ---------------------------------------------------------
say ""
say "==> Done. Next steps:"
cat <<EOF

1. Configure the hub:
     sudo install -m 0644 "$PREFIX/plan-hub.env.example" /etc/plan-hub.env
     sudoedit /etc/plan-hub.env
   Set HUB_WORKSTATIONS, HUB_ARCHIVES, HUB_LABELS and HUB_PUBLIC_SUFFIX.
   The port ranges in HUB_WORKSTATIONS must NOT overlap between workstations.

2. Start the hub:
     sudo systemctl daemon-reload
     sudo systemctl enable --now plan-hub
     curl -fsS http://127.0.0.1:8899/healthz     # expected: ok

3. Put it behind TLS and an OAuth gate (not installed by this script):
     - reverse-proxy 127.0.0.1:8899 through nginx
     - require SSO (e.g. oauth2-proxy) on every location
     - set server_name to the names you publish

4. Session links are port-derived: https://p<port>.<HUB_PUBLIC_SUFFIX>/
   Issue ONE multi-SAN certificate covering every p<port>.<suffix> host,
   then create DNS records pointing each of those names at the hub.

5. Deploy the per-workstation pieces on each workstation:
     - copy "$PREFIX/workstation/plannotator-update.sh" to ~/.local/bin/
     - adapt and install "$PREFIX"/systemd/plannotator-update.{service,timer}
       and plannotator-archive.service into ~/.config/systemd/user/
       (edit the /home/user/... paths for the real user)
     - systemctl --user enable --now plannotator-archive plannotator-update.timer

The hub only scans and aggregates; it never proxies live review traffic.
EOF
