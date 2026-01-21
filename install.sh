#!/usr/bin/env bash
#
# Install script for murl
# Usage: curl -sSL https://raw.githubusercontent.com/turlockmike/murl/main/install.sh | bash
#

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}Installing murl...${NC}"

# Check if Python is installed
if ! command -v python3 &> /dev/null && ! command -v python &> /dev/null; then
    echo -e "${RED}Error: Python 3 is required but not installed.${NC}"
    echo "Please install Python 3.8 or later and try again."
    exit 1
fi

# Determine Python command
if command -v python3 &> /dev/null; then
    PYTHON_CMD="python3"
else
    PYTHON_CMD="python"
fi

# Check Python version
PYTHON_VERSION=$($PYTHON_CMD -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
PYTHON_MAJOR=$($PYTHON_CMD -c 'import sys; print(sys.version_info.major)')
PYTHON_MINOR=$($PYTHON_CMD -c 'import sys; print(sys.version_info.minor)')

if [[ "$PYTHON_MAJOR" -lt 3 ]] || [[ "$PYTHON_MAJOR" -eq 3 && "$PYTHON_MINOR" -lt 10 ]]; then
    echo -e "${RED}Error: Python 3.10 or later is required.${NC}"
    echo "Found: Python $PYTHON_VERSION"
    exit 1
fi

echo -e "${GREEN}✓ Python $PYTHON_VERSION detected${NC}"

# Check if pip is installed
if ! $PYTHON_CMD -m pip --version &> /dev/null; then
    echo -e "${YELLOW}Warning: pip is not installed.${NC}"
    echo "Please install pip using your system package manager:"
    echo "  - Ubuntu/Debian: sudo apt-get install python3-pip"
    echo "  - Fedora/RHEL: sudo dnf install python3-pip"
    echo "  - macOS: python3 -m ensurepip --upgrade"
    exit 1
fi

echo -e "${GREEN}Installing murl via pip...${NC}"

# Install murl
if [[ "$EUID" -eq 0 ]]; then
    # Running as root
    $PYTHON_CMD -m pip install --upgrade murl
    echo -e "${GREEN}✓ murl installed successfully${NC}"
else
    # Running as user - try with --user flag
    if $PYTHON_CMD -m pip install --user --upgrade murl; then
        echo -e "${GREEN}✓ murl installed successfully${NC}"
        
        # Check if user's local bin is in PATH
        USER_BIN="$HOME/.local/bin"
        if [[ -d "$USER_BIN" && ":$PATH:" != *":$USER_BIN:"* ]]; then
            echo -e "${YELLOW}Warning: $USER_BIN is not in your PATH${NC}"
            echo "Add the following line to your ~/.bashrc, ~/.zshrc, or ~/.profile:"
            echo ""
            echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
            echo ""
        fi
    else
        # If --user fails, try with sudo
        echo -e "${YELLOW}User installation failed. Trying with sudo...${NC}"
        if command -v sudo &> /dev/null; then
            sudo $PYTHON_CMD -m pip install --upgrade murl
            echo -e "${GREEN}✓ murl installed successfully${NC}"
        else
            echo -e "${RED}Error: Cannot install murl. Please run as root or install pip for your user.${NC}"
            exit 1
        fi
    fi
fi

# Verify installation
if command -v murl &> /dev/null; then
    VERSION=$(murl --version 2>&1 | head -n1 || echo "unknown")
    echo -e "${GREEN}✓ Installation complete!${NC}"
    echo -e "murl version: $VERSION"
    echo ""
    echo "Usage: murl http://localhost:3000/tools"
    echo "Help:  murl --help"
else
    echo -e "${YELLOW}Installation completed, but 'murl' command not found in PATH.${NC}"
    echo "You may need to add the Python scripts directory to your PATH."
fi
