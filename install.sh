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

# Cleanup function
cleanup() {
    if [[ -n "$TEMP_DIR" && -d "$TEMP_DIR" ]]; then
        rm -rf "$TEMP_DIR"
    fi
}

# Set trap to cleanup on exit
trap cleanup EXIT

echo -e "${GREEN}Installing murl...${NC}"

# Check if Python is installed
if ! command -v python3 &> /dev/null && ! command -v python &> /dev/null; then
    echo -e "${RED}Error: Python 3 is required but not installed.${NC}"
    echo "Please install Python 3.10 or later and try again."
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

# Check if curl is installed
if ! command -v curl &> /dev/null; then
    echo -e "${RED}Error: curl is required but not installed.${NC}"
    echo "Please install curl using your system package manager:"
    echo "  - Ubuntu/Debian: sudo apt-get install curl"
    echo "  - Fedora/RHEL: sudo dnf install curl"
    echo "  - macOS: curl is pre-installed"
    exit 1
fi

echo -e "${GREEN}Downloading murl from GitHub releases...${NC}"

# Get the latest release version using GitHub API
GITHUB_API_URL="https://api.github.com/repos/turlockmike/murl/releases/latest"
RELEASE_JSON=$(curl -fsSL "$GITHUB_API_URL" 2>&1)
CURL_EXIT_CODE=$?

if [[ $CURL_EXIT_CODE -ne 0 ]]; then
    echo -e "${RED}Error: Failed to fetch release information from GitHub API${NC}"
    echo "Please check your internet connection and try again."
    exit 1
fi

LATEST_RELEASE=$(echo "$RELEASE_JSON" | grep '"tag_name":' | sed -E 's/.*"v([^"]+)".*/\1/' | head -n1)

if [[ -z "$LATEST_RELEASE" ]]; then
    echo -e "${RED}Error: Failed to parse latest release version${NC}"
    echo "Please report this issue at https://github.com/turlockmike/murl/issues"
    exit 1
fi

echo -e "${GREEN}Latest version: $LATEST_RELEASE${NC}"

# Create temporary directory
TEMP_DIR=$(mktemp -d)
if [[ -z "$TEMP_DIR" || ! -d "$TEMP_DIR" ]]; then
    echo -e "${RED}Error: Failed to create temporary directory${NC}"
    exit 1
fi

cd "$TEMP_DIR"

# Download the wheel file from the latest release
WHEEL_URL="https://github.com/turlockmike/murl/releases/download/v${LATEST_RELEASE}/mcp_curl-${LATEST_RELEASE}-py3-none-any.whl"

echo -e "${GREEN}Downloading from: $WHEEL_URL${NC}"

if ! curl -fL -o "mcp_curl-${LATEST_RELEASE}-py3-none-any.whl" "$WHEEL_URL"; then
    echo -e "${RED}Error: Failed to download release${NC}"
    echo "URL: $WHEEL_URL"
    echo "This might mean the release doesn't have the expected wheel file."
    exit 1
fi

echo -e "${GREEN}Installing murl from GitHub release...${NC}"

# Install murl
if [[ "$EUID" -eq 0 ]]; then
    # Running as root
    $PYTHON_CMD -m pip install "mcp_curl-${LATEST_RELEASE}-py3-none-any.whl"
    echo -e "${GREEN}✓ murl installed successfully${NC}"
else
    # Running as user - try with --user flag
    if $PYTHON_CMD -m pip install --user "mcp_curl-${LATEST_RELEASE}-py3-none-any.whl"; then
        echo -e "${GREEN}✓ murl installed successfully${NC}"
        
        # Check if user's local bin is in PATH
        USER_BIN="$HOME/.local/bin"
        if [[ -d "$USER_BIN" && ":$PATH:" != *":$USER_BIN:"* ]]; then
            # Detect user's shell for appropriate rc file
            RC_FILE="$HOME/.bashrc"
            if [[ -n "$SHELL" ]]; then
                case "$SHELL" in
                    */zsh)
                        RC_FILE="$HOME/.zshrc"
                        ;;
                    */bash)
                        RC_FILE="$HOME/.bashrc"
                        ;;
                esac
            fi
            
            echo -e "${YELLOW}Warning: $USER_BIN is not in your PATH${NC}"
            echo ""
            echo "Add the following to your $RC_FILE:"
            echo ""
            echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
            echo ""
            echo "Then run:"
            echo ""
            echo "  source $RC_FILE"
            echo "  murl --version"
            echo ""
        fi
    else
        # If --user fails, try with sudo
        echo -e "${YELLOW}User installation failed. Trying with sudo...${NC}"
        if command -v sudo &> /dev/null; then
            sudo $PYTHON_CMD -m pip install "mcp_curl-${LATEST_RELEASE}-py3-none-any.whl"
            echo -e "${GREEN}✓ murl installed successfully${NC}"
        else
            echo -e "${RED}Error: Cannot install murl. Please run as root or install pip for your user.${NC}"
            exit 1
        fi
    fi
fi

# Function to detect shell and provide PATH instructions
provide_path_instructions() {
    local bin_dir="$1"
    
    # Detect user's shell
    local rc_file="$HOME/.bashrc"
    
    if [[ -n "$SHELL" ]]; then
        case "$SHELL" in
            */zsh)
                rc_file="$HOME/.zshrc"
                ;;
            */bash)
                rc_file="$HOME/.bashrc"
                ;;
            *)
                # Default to bash if unknown
                rc_file="$HOME/.bashrc"
                ;;
        esac
    fi
    
    echo -e "${YELLOW}Installation completed, but 'murl' command not found in PATH.${NC}"
    echo ""
    echo "Add the following to your $rc_file:"
    echo ""
    echo "  export PATH=\"$bin_dir:\$PATH\""
    echo ""
    echo "Then run:"
    echo ""
    echo "  source $rc_file"
    echo "  murl --version"
    echo ""
}

# Verify installation
if command -v murl &> /dev/null; then
    VERSION=$(murl --version 2>&1 | head -n1 || echo "unknown")
    echo -e "${GREEN}✓ Installation complete!${NC}"
    echo -e "murl version: $VERSION"
    echo ""
    echo "Usage: murl http://localhost:3000/tools"
    echo "Help:  murl --help"
else
    # Determine where pip installed the package
    PYTHON_SCRIPTS_DIR=""
    
    # Try to get the user scripts directory from Python
    if [[ "$EUID" -ne 0 ]]; then
        # For user installation, try to get the user base bin directory
        PYTHON_SCRIPTS_DIR=$($PYTHON_CMD -c "import site; import os; print(os.path.join(site.USER_BASE, 'bin'))" 2>/dev/null)
        
        # If that doesn't exist or is empty, try common locations
        if [[ -z "$PYTHON_SCRIPTS_DIR" || ! -d "$PYTHON_SCRIPTS_DIR" ]]; then
            # Check common user bin locations
            if [[ -d "$HOME/.local/bin" ]]; then
                PYTHON_SCRIPTS_DIR="$HOME/.local/bin"
            elif [[ -d "$HOME/Library/Python/$PYTHON_VERSION/bin" ]]; then
                PYTHON_SCRIPTS_DIR="$HOME/Library/Python/$PYTHON_VERSION/bin"
            fi
        fi
    else
        # For system installation, check common system bin directories
        for dir in /usr/local/bin /usr/bin; do
            if [[ -d "$dir" ]]; then
                PYTHON_SCRIPTS_DIR="$dir"
                break
            fi
        done
    fi
    
    # If we found a scripts directory, provide instructions
    if [[ -n "$PYTHON_SCRIPTS_DIR" ]]; then
        provide_path_instructions "$PYTHON_SCRIPTS_DIR"
    else
        # Fallback to generic message
        echo -e "${YELLOW}Installation completed, but 'murl' command not found in PATH.${NC}"
        echo "You may need to add the Python scripts directory to your PATH."
    fi
fi
