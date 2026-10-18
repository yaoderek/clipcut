#!/bin/sh
set -e
mkdir -p /app/runs
for d in /app/preload/*; do [ -d "$d" ] && [ ! -e "/app/runs/$(basename "$d")" ] && cp -r "$d" /app/runs/ && echo "seeded $(basename "$d")"; done
exec python app.py
