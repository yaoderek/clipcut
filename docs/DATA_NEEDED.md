# Data needed to replace the heuristic scorer

The current scorer (`dummy-heuristic-v0`) is **not trained**. Learning how edits (hook timing, cut pace, captions, length) affect retention requires **raw video paired with per-viewer watch behavior for the same clip**. No public dataset has both:
- KuaiRand-Pure: real per-user watch time, but no video (ids and duration only).
- MicroLens: raw video, but only likes/views and interaction pairs (no watch time).
- YouTube Shorts scraping: public view/like counts only; blocked from the box.

Needed, in order of value:
1. **YouTube Analytics API** (`audienceWatchRatio` / relative retention per video, `averageViewPercentage`, shares). This is available only for channels Derek or partners own. Pair it with the uploaded files.
2. **SideShift creator data**: the posted clip file plus per-post metrics (3-s view rate, average watch %, shares) from TikTok/Reels/Shorts creator dashboards, exported per variant.
3. **Our own A/B tests**: post several ClipCut variants of one source. This gives a within-source comparison, which controls for topic and creator.

Minimum useful: ~200–500 clips with retention curves, ≥3 variants per source. Per-viewer logs (or audience segments) are needed for real personas.
