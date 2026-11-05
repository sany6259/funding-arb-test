#!/usr/bin/env bash
# Funding Arb Dashboard — Unified Launch Script
#
# Usage:
#   bash start.sh              # Browser mode (auto-build frontend + start server)
#   bash start.sh --desktop    # Desktop app mode (Tauri)
#   bash start.sh --api-only   # API backend only
#   bash start.sh --build-web  # Build frontend only, do not start

set -euo pipefail
cd "$(dirname "$0")"

# ---------------------------------------------------------------------------
# Colors
# ---------------------------------------------------------------------------
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${CYAN}  → $1${NC}"; }
ok()    { echo -e "${GREEN}  ✓ $1${NC}"; }
warn()  { echo -e "${YELLOW}  ⚠ $1${NC}"; }
error() { echo -e "${RED}  ✗ $1${NC}"; exit 1; }

# ---------------------------------------------------------------------------
# Check dependencies
# ---------------------------------------------------------------------------
check_python() {
    if [ ! -x .venv/bin/python ]; then
        info "Preparing local Python environment..."
        bash ./setup.sh
    fi
    PYTHON="$(pwd)/.venv/bin/python"
    "$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 12))' || error "Python 3.12+ is required in .venv."
    ok "Python: $("$PYTHON" --version)"
}

check_node() {
    command -v node >/dev/null && command -v npm >/dev/null || error "Node.js and npm are required (Node 20.19+ or 22.12+)."
    node -e 'const [a,b]=process.versions.node.split(".").map(Number); process.exit(((a===20 && b>=19)||(a===22 && b>=12)||a>22) ? 0 : 1)' || error "Node.js 20.19+ or 22.12+ is required (Node 21 is unsupported)."
    ok "Node.js: $(node --version)"
}

check_rust() {
    if ! command -v rustc &>/dev/null; then
        error "Rust is not installed. Please run: curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh"
    fi
    ok "Rust: $(rustc --version)"
}

install_python_deps() {
    # Enforce the declared versions even when all imports happen to succeed.
    info "Checking Python dependencies in .venv..."
    "$PYTHON" -m pip install -r server/requirements.txt
    "$PYTHON" -m pip check || error "Python dependencies are inconsistent."
    "$PYTHON" -c "import fastapi, uvicorn, websockets, requests" || error "Python dependencies are incomplete."
    ok "Python dependencies ready"
}

install_node_deps() {
    [ -f web/package-lock.json ] || error "web/package-lock.json is required for npm ci."
    info "Installing locked Node.js dependencies..."
    (cd web && npm ci --include=optional --no-audit --no-fund) || error "npm ci failed."
    ok "Node.js dependencies ready"
}

check_hyperliquid_repo() {
    # Live Hyperliquid order signing reuses the sibling hyperliquid skill repo.
    if [ -d "../hyperliquid/scripts" ]; then
        ok "Hyperliquid skill repo found (live trading available)"
    else
        warn "../hyperliquid repo not found — Hyperliquid scan/dry-run only, live orders disabled"
    fi
}

build_web() {
    info "Building frontend..."
    (cd web && npm run build) || error "Frontend build failed."
    ok "Frontend build complete → web/dist/"
}

# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------
MODE="${1:-browser}"

case "$MODE" in
    --desktop)
        echo ""
        echo "  ╔══════════════════════════════════════╗"
        echo "  ║   Funding Arb — Desktop App Mode     ║"
        echo "  ╚══════════════════════════════════════╝"
        echo ""
        check_python
        check_node
        check_rust
        install_python_deps
        install_node_deps
        info "Starting Tauri desktop app..."
        cd web && npm run tauri dev
        ;;

    --api-only)
        echo ""
        echo "  ╔══════════════════════════════════════╗"
        echo "  ║   Funding Arb — API Only Mode         ║"
        echo "  ╚══════════════════════════════════════╝"
        echo ""
        check_python
        install_python_deps
        check_hyperliquid_repo
        info "Starting API server..."
        exec "$PYTHON" scripts/launch_backend.py --host 127.0.0.1 --port 8787
        ;;

    --build-web)
        echo ""
        echo "  ╔══════════════════════════════════════╗"
        echo "  ║   Funding Arb — Build Frontend        ║"
        echo "  ╚══════════════════════════════════════╝"
        echo ""
        check_node
        install_node_deps
        build_web
        ok "Done. Run bash start.sh to start in browser mode"
        ;;

    --help|-h)
        echo ""
        echo "  Funding Arb Dashboard Launch Script"
        echo ""
        echo "  Usage:"
        echo "    bash start.sh                Browser mode (build frontend + start server)"
        echo "    bash start.sh --desktop      Desktop app mode (Tauri, requires Rust)"
        echo "    bash start.sh --api-only     API backend only"
        echo "    bash start.sh --build-web    Build frontend only"
        echo ""
        ;;

    browser|"")
        echo ""
        echo "  ╔══════════════════════════════════════╗"
        echo "  ║   Funding Arb — Browser Mode         ║"
        echo "  ╚══════════════════════════════════════╝"
        echo ""
        check_python
        check_node
        install_python_deps
        install_node_deps
        check_hyperliquid_repo

        # Auto-build frontend if not yet built
        if [ ! -f "web/dist/index.html" ]; then
            warn "Frontend not built, building now..."
            build_web
        fi

        info "Starting server..."
        echo ""
        exec "$PYTHON" scripts/launch_backend.py --host 127.0.0.1 --port 8787
        ;;

    *)
        error "Unknown argument: $MODE. Run bash start.sh --help for usage"
        ;;
esac
