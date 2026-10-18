#!/bin/sh
# Download demo sample videos and preload-run variant mp4s/posters.
# Idempotent: files already on disk are left alone. Offline prints a warning and exits 0.
cd "$(dirname "$0")/.." || exit 0
exec python3 cli.py demo-setup
