#!/usr/bin/env bash
# Shell wrapper for Python bootstrap
# Uses venv for isolated dependencies

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

# Check for Python 3.10+
check_python() {
    local python_cmd=""

    for cmd in python3 python; do
        if command -v "$cmd" &>/dev/null; then
            version=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null)
            major=$(echo "$version" | cut -d. -f1)
            minor=$(echo "$version" | cut -d. -f2)
            if [ "$major" -ge 3 ] && [ "$minor" -ge 10 ]; then
                python_cmd="$cmd"
                break
            fi
        fi
    done

    if [ -z "$python_cmd" ]; then
        echo "Error: Python 3.10+ required"
        echo "Install with: brew install python@3.11"
        exit 1
    fi

    echo "$python_cmd"
}

# Setup venv if needed
setup_venv() {
    local python_cmd="$1"

    if [ ! -d "$VENV_DIR" ]; then
        echo "Setting up Python environment..."
        "$python_cmd" -m venv "$VENV_DIR"
        "$VENV_DIR/bin/pip" install --quiet --upgrade pip
        "$VENV_DIR/bin/pip" install --quiet -e "$SCRIPT_DIR"
    fi
}

# Main
PYTHON=$(check_python)
setup_venv "$PYTHON"

# Run the bootstrap module
exec "$VENV_DIR/bin/python" -m bootstrap "$@"
