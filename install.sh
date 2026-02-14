#!/usr/bin/env bash
# ============================================================================
# Argus — Install Script
# Sets up the system user, virtual environment, systemd service, and starts
# the Argus health-monitoring daemon.
#
# Usage:  sudo bash install.sh
# ============================================================================

set -euo pipefail  # Exit on error, undefined vars, and pipe failures

# Paths and naming — change these if you want a custom install location
INSTALL_DIR="/opt/argus"
LOG_DIR="/var/log/argus"
SERVICE_NAME="argus"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"  # directory where this script lives

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: This script must be run as root (use sudo)." >&2
    exit 1
fi

if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 is not installed." >&2
    exit 1
fi

# Enforce Python 3.10+ (required for type hint syntax used in argus.py)
PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)

DISTRO_ID="unknown"
if [[ -f /etc/os-release ]]; then
    . /etc/os-release
    DISTRO_ID="${ID:-unknown}"
fi

if [[ "$PYTHON_MAJOR" -lt 3 ]] || { [[ "$PYTHON_MAJOR" -eq 3 ]] && [[ "$PYTHON_MINOR" -lt 10 ]]; }; then
    echo "ERROR: Python 3.10+ is required (found $PYTHON_VERSION)." >&2
    exit 1
fi

install_venv_dependencies() {
    case "$DISTRO_ID" in
        ubuntu|debian|linuxmint|pop)
            export DEBIAN_FRONTEND=noninteractive
            apt-get update -y
            if ! apt-get install -y "python${PYTHON_VERSION}-venv"; then
                apt-get install -y python3-venv
            fi
            ;;
        fedora|rhel|centos|rocky|almalinux)
            if command -v dnf &>/dev/null; then
                dnf install -y "python${PYTHON_MAJOR}-pip" || dnf install -y python3-pip
            elif command -v yum &>/dev/null; then
                yum install -y "python${PYTHON_MAJOR}-pip" || yum install -y python3-pip
            else
                return 1
            fi
            ;;
        arch|manjaro|endeavouros)
            pacman -Sy --noconfirm python
            ;;
        opensuse*|sles)
            zypper --non-interactive install "python${PYTHON_MAJOR}-pip" || zypper --non-interactive install python3-pip
            ;;
        alpine)
            apk add --no-cache python3
            ;;
        *)
            return 1
            ;;
    esac
}

# Ensure the venv module is available (not always installed by default)
if ! python3 -c "import venv" &>/dev/null; then
    echo "Python venv module is missing - attempting automatic installation..."
    if ! install_venv_dependencies || ! python3 -c "import venv" &>/dev/null; then
        echo "ERROR: Python venv module is not installed and auto-install failed." >&2
        echo "       Install the venv package for your distribution, then rerun install.sh." >&2
        exit 1
    fi
fi

# ---------------------------------------------------------------------------
# Create system user
# ---------------------------------------------------------------------------

# Create a dedicated system user with no login shell for security.
# The daemon runs as this user instead of root.
if ! id "$SERVICE_NAME" &>/dev/null; then
    echo "Creating system user '$SERVICE_NAME'..."

    # Find nologin shell location (varies by distro)
    NOLOGIN_SHELL="/usr/sbin/nologin"
    if [[ ! -f "$NOLOGIN_SHELL" ]] && [[ -f "/sbin/nologin" ]]; then
        NOLOGIN_SHELL="/sbin/nologin"
    elif [[ ! -f "$NOLOGIN_SHELL" ]]; then
        NOLOGIN_SHELL="/bin/false"
    fi

    useradd --system --no-create-home --shell "$NOLOGIN_SHELL" "$SERVICE_NAME"
else
    echo "System user '$SERVICE_NAME' already exists."
fi

# ---------------------------------------------------------------------------
# Create directories
# ---------------------------------------------------------------------------

echo "Setting up directories..."

mkdir -p "$INSTALL_DIR"
mkdir -p "$LOG_DIR"

chown "$SERVICE_NAME":"$SERVICE_NAME" "$LOG_DIR"
chmod 750 "$LOG_DIR"  # owner rwx, group rx, others none

# ---------------------------------------------------------------------------
# Copy application files
# ---------------------------------------------------------------------------

echo "Copying application files to $INSTALL_DIR..."

cp "$SCRIPT_DIR/argus.py" "$INSTALL_DIR/argus.py"
cp "$SCRIPT_DIR/requirements.txt" "$INSTALL_DIR/requirements.txt"

# Copy .env if it exists in the source directory; otherwise fall back to the
# example template. Never overwrite an existing .env in the install directory
# to avoid destroying a working configuration on reinstall.
if [[ -f "$SCRIPT_DIR/.env" ]]; then
    cp "$SCRIPT_DIR/.env" "$INSTALL_DIR/.env"
elif [[ ! -f "$INSTALL_DIR/.env" ]]; then
    cp "$SCRIPT_DIR/.env.example" "$INSTALL_DIR/.env"
    echo ""
    echo "WARNING: No .env file found — copied .env.example to $INSTALL_DIR/.env"
    echo "         Edit $INSTALL_DIR/.env with your actual configuration before starting."
    echo ""
fi

chmod 600 "$INSTALL_DIR/.env"  # restrict .env to owner only (contains secrets)
chown -R "$SERVICE_NAME":"$SERVICE_NAME" "$INSTALL_DIR"

# ---------------------------------------------------------------------------
# Python virtual environment & dependencies
# ---------------------------------------------------------------------------

echo "Setting up Python virtual environment..."

create_venv() {
    python3 -m venv "$INSTALL_DIR/venv"
}

# Create venv only if it doesn't already exist (safe for reinstalls)
if [[ ! -d "$INSTALL_DIR/venv" ]]; then
    if ! create_venv; then
        echo "Initial venv creation failed - attempting to install missing dependencies..."
        if install_venv_dependencies; then
            create_venv
        else
            echo "ERROR: Failed to create Python virtual environment." >&2
            echo "       Could not auto-install venv dependencies for this distribution." >&2
            exit 1
        fi
    fi
fi

# Validate venv is complete - if pip is missing, the venv is corrupted
if [[ ! -f "$INSTALL_DIR/venv/bin/pip" ]]; then
    echo "Incomplete venv detected - recreating..."
    rm -rf "$INSTALL_DIR/venv"
    if ! create_venv; then
        echo "Recreation failed - attempting to install missing dependencies..."
        if install_venv_dependencies; then
            create_venv
        else
            echo "ERROR: Failed to recreate Python virtual environment." >&2
            echo "       Could not auto-install venv dependencies for this distribution." >&2
            exit 1
        fi
    fi
fi

# Verify venv creation succeeded
if [[ ! -f "$INSTALL_DIR/venv/bin/pip" ]]; then
    echo "ERROR: Failed to create Python virtual environment." >&2
    echo "       The venv module may not be functioning correctly." >&2
    exit 1
fi

"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"

chown -R "$SERVICE_NAME":"$SERVICE_NAME" "$INSTALL_DIR/venv"

# ---------------------------------------------------------------------------
# Systemd service
# ---------------------------------------------------------------------------

# Check if systemd is available
if ! command -v systemctl &>/dev/null; then
    echo "" >&2
    echo "ERROR: systemd is not available on this system." >&2
    echo "       Argus requires systemd for service management." >&2
    echo "" >&2
    echo "       The application files have been installed to $INSTALL_DIR" >&2
    echo "       You will need to manually configure your init system to run:" >&2
    echo "       $INSTALL_DIR/venv/bin/python $INSTALL_DIR/argus.py" >&2
    echo "" >&2
    exit 1
fi

echo "Installing systemd service..."

cp "$SCRIPT_DIR/argus.service" /etc/systemd/system/argus.service
systemctl daemon-reload
systemctl enable argus.service

echo "Starting Argus..."
systemctl start argus.service

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

echo ""
echo "============================================================"
echo " Argus installed successfully."
echo " The Hundred-Eyed Monitor is now watching."
echo "============================================================"
echo ""
echo " Config:   $INSTALL_DIR/.env"
echo " Logs:     $LOG_DIR/argus.log"
echo " Journal:  journalctl -u argus -f"
echo " Status:   systemctl status argus"
echo ""
