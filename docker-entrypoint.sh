#!/bin/sh
set -e
mkdir -p /app/runs
# runs/ is the Fly volume. Copy a missing preload run, and fill files the volume
# does not have yet (metrics.json, demo.json) without overwriting existing ones.
if [ -d /app/preload ]; then
  for d in /app/preload/*; do
    [ -d "$d" ] || continue
    name=$(basename "$d")
    dest="/app/runs/$name"
    if [ ! -e "$dest" ]; then
      cp -a "$d" "$dest"
      echo "seeded $name"
    else
      find "$d" -type f | while IFS= read -r f; do
        rel=${f#"$d"/}
        if [ ! -e "$dest/$rel" ]; then
          mkdir -p "$(dirname "$dest/$rel")"
          cp -a "$f" "$dest/$rel"
          echo "seeded $name/$rel"
        fi
      done
    fi
  done
fi
exec python app.py
