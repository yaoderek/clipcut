# Demo sample sources

These files are downloaded by `python cli.py demo-setup` (or `make demo` / `scripts/fetch_demo.sh`). They are not committed. `*.mp4` is gitignored.

| File | Source | License |
| --- | --- | --- |
| `talking_head_niosh.mp4` | Live app `GET /api/samples` → `/samples/talking_head_niosh.mp4` on https://clipcut-dubhacks.fly.dev when that file is served. The current app only lists the name, so the fallback is seconds 28–148 of [Science Speaks – Liz, Epidemiologist](https://commons.wikimedia.org/wiki/File:Science_Speaks_-_Talking_to_Women_in_Science_(Liz,_Epidemiologist).webm) (NIOSH / CDC, Wikimedia Commons). Direct file: https://upload.wikimedia.org/wikipedia/commons/7/7b/Science_Speaks_-_Talking_to_Women_in_Science_%28Liz%2C_Epidemiologist%29.webm | Public domain (PD US HHS CDC NIOSH) |

Variant mp4s and posters for `runs/v2_niosh_multi` are fetched from `https://clipcut-dubhacks.fly.dev/runs/v2_niosh_multi/` (`vNN.mp4`, `.vNN_poster.jpg`). Those are renders of the NIOSH clip above.
