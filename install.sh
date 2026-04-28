#!/bin/bash
set -e

INSTALL_DIR="$HOME/.pathos"
BIN_LINK="/usr/local/bin/pathos"
REPO="stefanopochet/pathos"

# --- Helpers ---

die() { echo "Error: $1" >&2; exit 1; }

check_deps() {
    local missing=""
    command -v python3 >/dev/null 2>&1 || missing="$missing python3"
    command -v tmux >/dev/null 2>&1    || missing="$missing tmux"
    command -v claude >/dev/null 2>&1  || missing="$missing claude"

    if [ -n "$missing" ]; then
        die "Missing dependencies:$missing — install them and re-run."
    fi

    local py_minor
    py_minor=$(python3 -c "import sys; print(sys.version_info.minor)")
    if [ "$py_minor" -lt 10 ]; then
        die "Python 3.10+ required (found 3.$py_minor)"
    fi
}

# --- Install from local source (git clone) ---

install_local() {
    local script_dir="$1"
    echo "Installing from local source: $script_dir"

    mkdir -p "$INSTALL_DIR/src" "$INSTALL_DIR/logs"
    cp -r "$script_dir/src/pathos" "$INSTALL_DIR/src/"

    copy_example_config "$script_dir"
    setup_launcher
    echo ""
    echo "pathos installed to $INSTALL_DIR"
    echo "Logs:   $INSTALL_DIR/logs/"
    echo "Config: $INSTALL_DIR/config.yml (edit to customize)"
}

# --- Install from GitHub release ---

install_remote() {
    local version="$1"
    local tarball_url

    if [ -z "$version" ]; then
        echo "Fetching latest release..."
        tarball_url=$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" \
            | python3 -c "import sys,json; print(json.load(sys.stdin)['tarball_url'])")
        version=$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" \
            | python3 -c "import sys,json; print(json.load(sys.stdin)['tag_name'])")
    else
        tarball_url="https://api.github.com/repos/$REPO/tarball/$version"
    fi

    echo "Installing pathos $version from GitHub..."

    local tmpdir
    tmpdir=$(mktemp -d)
    trap "rm -rf '$tmpdir'" EXIT

    curl -fsSL "$tarball_url" | tar xz -C "$tmpdir" --strip-components=1

    mkdir -p "$INSTALL_DIR/src" "$INSTALL_DIR/logs"
    rm -rf "$INSTALL_DIR/src/pathos"
    cp -r "$tmpdir/src/pathos" "$INSTALL_DIR/src/"

    copy_example_config "$tmpdir"
    setup_launcher
    echo ""
    echo "pathos $version installed to $INSTALL_DIR"
    echo "Logs:   $INSTALL_DIR/logs/"
    echo "Config: $INSTALL_DIR/config.yml (edit to customize)"
}

# --- Copy example config on first install ---

copy_example_config() {
    local source_dir="$1"
    if [ ! -f "$INSTALL_DIR/config.yml" ] && [ -f "$source_dir/config.example.yml" ]; then
        cp "$source_dir/config.example.yml" "$INSTALL_DIR/config.yml"
        echo "Created config: $INSTALL_DIR/config.yml"
    fi
}

# --- Shared setup ---

setup_launcher() {
    cat > "$INSTALL_DIR/pathos" << 'LAUNCHER'
#!/bin/bash
exec python3 -m pathos "$@"
LAUNCHER
    chmod +x "$INSTALL_DIR/pathos"

    cat > "$INSTALL_DIR/bin-wrapper" << WRAPPER
#!/bin/bash
export PYTHONPATH="$INSTALL_DIR/src"
exec "$INSTALL_DIR/pathos" "\$@"
WRAPPER
    chmod +x "$INSTALL_DIR/bin-wrapper"

    if [ -d "/usr/local/bin" ]; then
        ln -sf "$INSTALL_DIR/bin-wrapper" "$BIN_LINK"
        echo "Linked: $BIN_LINK → pathos"
    elif [ -d "$HOME/.local/bin" ]; then
        ln -sf "$INSTALL_DIR/bin-wrapper" "$HOME/.local/bin/pathos"
        echo "Linked: ~/.local/bin/pathos"
    else
        echo "Add $INSTALL_DIR to your PATH manually"
    fi
}

# --- Main ---

echo "pathos installer"
echo "================"

check_deps
echo "Dependencies OK"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ -f "$SCRIPT_DIR/src/pathos/__init__.py" ]; then
    install_local "$SCRIPT_DIR"
else
    install_remote "${1:-}"
fi

echo ""
echo "Run:    pathos"
echo "Debug:  PATHOS_DEBUG=1 pathos"
echo "Update: pathos update"
