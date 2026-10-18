# Demo sample sources

These files are downloaded by `python cli.py demo-setup` (or `make demo` / `scripts/fetch_demo.sh`). They are not committed. `*.mp4` is gitignored.

| File | Source | License |
| --- | --- | --- |
| `talking_head_niosh.mp4` | Live app `GET /api/samples` → `/samples/talking_head_niosh.mp4` on https://clipcut-dubhacks.fly.dev when that file is served. The current app only lists the name, so the fallback is seconds 28–148 of [Science Speaks – Liz, Epidemiologist](https://commons.wikimedia.org/wiki/File:Science_Speaks_-_Talking_to_Women_in_Science_(Liz,_Epidemiologist).webm) (NIOSH / CDC, Wikimedia Commons). Direct file: https://upload.wikimedia.org/wikipedia/commons/7/7b/Science_Speaks_-_Talking_to_Women_in_Science_%28Liz%2C_Epidemiologist%29.webm | Public domain (PD US HHS CDC NIOSH) |
| `talking_head_kendra.mp4` | Seconds 8–53 of [Science Speaks – Kendra, Industrial Hygienist](https://commons.wikimedia.org/wiki/File:Science_Speaks_-_Talking_to_Women_in_Science_(Kendra,_Industrial_Hygienist).webm). Direct file: https://upload.wikimedia.org/wikipedia/commons/e/ed/Science_Speaks_-_Talking_to_Women_in_Science_%28Kendra%2C_Industrial_Hygienist%29.webm | Public domain (PD US HHS CDC NIOSH) |
| `talking_head_nasa_calvin.mp4` | Seconds 2–47 of [NASA & NOAA Interview – Kate Calvin, 2024 global temperature](https://commons.wikimedia.org/wiki/File:NASA_%26_NOAA_Interview_Opportunity-_Snapshot_of_Earth_in_2024_Through_Temperature_Live_Shots_(SVS14742_-_KATE_CALVIN_2024_GLOBAL_TEMP_UPDATE).webm). Direct file: https://upload.wikimedia.org/wikipedia/commons/7/73/NASA_%26_NOAA_Interview_Opportunity-_Snapshot_of_Earth_in_2024_Through_Temperature_Live_Shots_%28SVS14742_-_KATE_CALVIN_2024_GLOBAL_TEMP_UPDATE%29.webm | Public domain (NASA) |

Variant mp4s and posters for `runs/v2_niosh_multi` are fetched from `https://clipcut-dubhacks.fly.dev/runs/v2_niosh_multi/` (`vNN.mp4`, `.vNN_poster.jpg`). Those are renders of the NIOSH Liz clip above. `runs/demo_pd_kendra` and `runs/demo_pd_calvin` are not listed until cutter v2 has rendered the Kendra and Calvin clips themselves. Metrics are simulated.
