#!/usr/bin/env bash
# Install handlecheck for the current user.
#
#   ~/.local/lib/handlecheck/source   the code, with its own virtualenv. Develop here.
#   ~/.local/bin/handlecheck          the command
#   $XDG_DATA_HOME/handlecheck        data, by default ~/.local/share/handlecheck
#
# Run as root, it also creates the locked account that browsers run under.
# To upgrade, run handlecheck --update.
set -euo pipefail
umask 022

SOURCE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
BROWSER_USER=handlecheck-browser
APP=$HOME/.local/lib/handlecheck/source
BIN=$HOME/.local/bin
case "${XDG_DATA_HOME:-}" in
    /*) DATA=$XDG_DATA_HOME/handlecheck ;;
    *) DATA=$HOME/.local/share/handlecheck ;;
esac

find_python() {
    local name
    for name in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
        if command -v "$name" >/dev/null &&
            "$name" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
            command -v "$name"
            return 0
        fi
    done
    return 1
}
PYTHON=$(find_python) || { echo "handlecheck needs Python 3.10 or newer" >&2; exit 1; }

if [ "$SOURCE" != "$APP" ]; then
    # The installed copy is a working repository, so never overwrite one.
    if [ -e "$APP/.git" ]; then
        echo "$APP is a git repository; update it there and run its install.sh" >&2
        exit 1
    fi
    echo "Copying to $APP"
    mkdir -p "$APP"
    find "$APP" -mindepth 1 -maxdepth 1 ! -name .venv -exec rm -rf {} +
    tar -C "$SOURCE" --exclude=./.venv --exclude='*.egg-info' \
        --exclude=__pycache__ --exclude=.pytest_cache -cf - . | tar -C "$APP" -xf -
fi

echo "Setting up $APP/.venv with $PYTHON"
"$PYTHON" -m venv "$APP/.venv"
"$APP/.venv/bin/python" -m pip install --quiet --upgrade pip
"$APP/.venv/bin/python" -m pip install --quiet -e "$APP[dev]" 2>/dev/null
mkdir -p "$BIN" "$DATA"
ln -sfn "$APP/.venv/bin/handlecheck" "$BIN/handlecheck"

if [ "$(id -u)" -eq 0 ]; then
    if ! id "$BROWSER_USER" >/dev/null 2>&1; then
        useradd --system --user-group --create-home --home-dir "/var/lib/$BROWSER_USER" \
            --shell "$(command -v nologin)" --comment "handlecheck browsers" "$BROWSER_USER"
        echo "Created user $BROWSER_USER"
    fi
    chmod 700 "$(getent passwd "$BROWSER_USER" | cut -d: -f6)"
fi

if ! command -v chromium >/dev/null && ! command -v chromium-browser >/dev/null &&
    ! command -v google-chrome >/dev/null && ! command -v firefox >/dev/null; then
    echo "warning: no browser found; only services with an API can be checked" >&2
fi
"$BIN/handlecheck" --version
echo "Code: $APP"
echo "Data: $DATA"
case ":$PATH:" in
    *":$BIN:"*) ;;
    *)
        echo
        echo "$BIN is not on your PATH, so the handlecheck command won't be found yet."
        echo "To add it, run:"
        echo
        case "${SHELL##*/}" in
            fish)
                echo "    fish_add_path \$HOME/.local/bin"
                ;;
            zsh)
                echo "    echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc"
                echo "    source ~/.zshrc"
                ;;
            bash)
                echo "    echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.bashrc"
                echo "    source ~/.bashrc"
                ;;
            *)
                echo "    echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.profile"
                echo "    . ~/.profile"
                ;;
        esac
        echo
        echo "Until then, you can run it as $BIN/handlecheck"
        ;;
esac
