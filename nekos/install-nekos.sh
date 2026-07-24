#!/bin/sh
# NekoShot Installation Script for nekOS (Void Linux)
# Run as root: sh nekos/install-nekos.sh

set -e

echo "========================================"
echo "NekoShot Installer for nekOS (Void Linux)"
echo "========================================"
echo

# Check if running as root
if [ "$(id -u)" -ne 0 ]; then
    echo "Error: Please run as root (sudo sh nekos/install-nekos.sh)"
    exit 1
fi

# This script lives in nekos/, one level below the repo root.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
INSTALL_DIR="/opt/nekoshot"
BIN_LINK="/usr/local/bin/nekoshot"

echo "[1/5] Installing system dependencies..."
xbps-install -Sy \
    python3 \
    rsync \
    zstd \
    tar \
    util-linux \
    parted

echo
echo "[2/5] Creating installation directory..."
mkdir -p "$INSTALL_DIR"

echo
echo "[3/5] Copying NekoShot files..."
cp -r "$REPO_DIR"/*.py "$INSTALL_DIR/"
cp -r "$REPO_DIR"/capture "$INSTALL_DIR/"
cp -r "$REPO_DIR"/restore "$INSTALL_DIR/"
cp -r "$REPO_DIR"/utils "$INSTALL_DIR/"

# Make main script executable
chmod +x "$INSTALL_DIR/nekoshot.py"

echo
echo "[4/5] Creating command wrapper..."
cat > "$BIN_LINK" << 'EOF'
#!/bin/sh
# NekoShot wrapper script
exec python3 /opt/nekoshot/nekoshot.py "$@"
EOF
chmod +x "$BIN_LINK"

echo
echo "[5/5] Creating default configuration directory..."
mkdir -p /etc/nekoshot
if [ ! -f /etc/nekoshot/config.json ]; then
    cat > /etc/nekoshot/config.json << 'EOF'
{
  "capture": {
    "output_dir": "/backup/nekoshot",
    "compression": "zstd",
    "compression_level": 3,
    "include_docker": true,
    "pause_docker": false
  },
  "restore": {
    "fix_bootloader": true,
    "restore_docker": true,
    "update_fstab": true
  },
  "retention": {
    "keep_count": 5
  },
  "log_level": "INFO"
}
EOF
    echo "Created default config at /etc/nekoshot/config.json"
fi

echo
echo "========================================"
echo "Installation Complete!"
echo "========================================"
echo
echo "Usage:"
echo "  sudo nekoshot capture --output /backup/"
echo "  sudo nekoshot restore --image /backup/image-xxx --target /mnt/restore"
echo "  nekoshot list --path /backup/"
echo "  nekoshot --help"
echo
echo "Configuration: /etc/nekoshot/config.json"
echo "Install location: $INSTALL_DIR"
echo
