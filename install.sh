#!/usr/bin/env bash
# Coomi installer — downloads the latest release binary for your platform.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/Septemc/Coomi/main/install.sh | bash
#
# Options:
#   --version <tag>   Install a specific version (e.g. v1.0.0). Default: latest.
#   --to <dir>        Install directory. Default: ~/.local/bin

set -euo pipefail

REPO="Septemc/Coomi"
BINARY_NAME="coomi"
VERSION="${COOMI_VERSION:-latest}"
INSTALL_DIR="${COOMI_INSTALL_DIR:-$HOME/.local/bin}"

# Parse arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        --version) VERSION="$2"; shift 2 ;;
        --to) INSTALL_DIR="$2"; shift 2 ;;
        --help|-h)
            echo "Coomi installer"
            echo ""
            echo "Usage: curl -fsSL https://raw.githubusercontent.com/${REPO}/main/install.sh | bash"
            echo ""
            echo "Options:"
            echo "  --version <tag>  Install specific version (e.g. v1.0.0)"
            echo "  --to <dir>       Install directory (default: ~/.local/bin)"
            echo "  --help           Show this help"
            exit 0
            ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

info() { echo "==> $*"; }
warn() { echo "Warning: $*" >&2; }
error() { echo "Error: $*" >&2; exit 1; }

# Detect OS
detect_os() {
    local os
    os="$(uname -s)"
    case "$os" in
        Linux*) echo "unknown-linux-gnu" ;;
        Darwin*) echo "apple-darwin" ;;
        MINGW*|MSYS*|CYGWIN*) echo "pc-windows-msvc" ;;
        *) error "Unsupported OS: $os" ;;
    esac
}

# Detect architecture
detect_arch() {
    local arch
    arch="$(uname -m)"
    case "$arch" in
        x86_64|amd64) echo "x86_64" ;;
        aarch64|arm64) echo "aarch64" ;;
        *) error "Unsupported architecture: $arch" ;;
    esac
}

# Resolve latest version tag from GitHub
resolve_version() {
    if [[ "$VERSION" == "latest" ]]; then
        VERSION="$(curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest" | grep '"tag_name"' | sed -E 's/.*"([^"]+)".*/\1/')"
        if [[ -z "$VERSION" ]]; then
            error "Could not determine latest version. Is the repo public?"
        fi
    fi
    info "Version: $VERSION"
}

# Build the download URL
build_url() {
    local os="$1"
    local arch="$2"
    local target="${arch}-${os}"
    local filename="${BINARY_NAME}-${target}"

    if [[ "$os" == *"windows"* ]]; then
        echo "https://github.com/${REPO}/releases/download/${VERSION}/${filename}.zip"
    else
        echo "https://github.com/${REPO}/releases/download/${VERSION}/${filename}.tar.gz"
    fi
}

# Download and install
install() {
    local os
    local arch
    os="$(detect_os)"
    arch="$(detect_arch)"

    resolve_version

    local url
    url="$(build_url "$os" "$arch")"

    info "Platform: ${arch}-${os}"
    info "Downloading: $url"

    local tmpdir
    tmpdir="$(mktemp -d)"
    trap 'rm -rf "$tmpdir"' EXIT

    if [[ "$os" == *"windows"* ]]; then
        curl -fsSL "$url" -o "$tmpdir/archive.zip"
        unzip -q -o "$tmpdir/archive.zip" -d "$tmpdir"
    else
        curl -fsSL "$url" -o "$tmpdir/archive.tar.gz"
        tar xzf "$tmpdir/archive.tar.gz" -C "$tmpdir"
    fi

    # Find the binary
    local binary
    if [[ "$os" == *"windows"* ]]; then
        binary="$(find "$tmpdir" -name "${BINARY_NAME}.exe" -type f | head -1)"
    else
        binary="$(find "$tmpdir" -name "${BINARY_NAME}" -type f -perm /u+x | head -1)"
        if [[ -z "$binary" ]]; then
            binary="$(find "$tmpdir" -name "${BINARY_NAME}" -type f | head -1)"
        fi
    fi

    [[ -n "$binary" ]] || error "Binary not found in archive"

    # Install directory
    mkdir -p "$INSTALL_DIR"

    # Install — overwrites any previous version
    local bin_name="${BINARY_NAME}"
    [[ "$os" == *"windows"* ]] && bin_name="${BINARY_NAME}.exe"
    local dest="$INSTALL_DIR/$bin_name"
    if [[ -x "$dest" ]]; then
        local existing_version
        existing_version="$("$dest" --version 2>/dev/null || true)"
        if [[ -n "$existing_version" ]]; then
            info "Upgrading from: $existing_version"
        else
            info "Overwriting existing installation at $dest"
        fi
    fi

    cp "$binary" "$dest"
    chmod +x "$dest"

    info "Installed to $dest"

    # Check PATH
    if ! echo "$PATH" | tr ':' '\n' | grep -q "^${INSTALL_DIR}$"; then
        warn "$INSTALL_DIR is not in your PATH."
        warn "Add this to your shell profile (~/.bashrc, ~/.zshrc, etc.):"
        echo ""
        echo "  export PATH=\"$INSTALL_DIR:\$PATH\""
        echo ""
    fi

    info "Done! Run '${BINARY_NAME}' to start."
}

install
