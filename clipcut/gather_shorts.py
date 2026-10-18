"""Data-gathering path (plan item 5, partly implemented): build an edit-level dataset for a niche from public Shorts.
  python -m clipcut.gather_shorts "startup tips #shorts" --n 50
For each result: yt-dlp metadata (views, likes, comments, duration, channel followers, upload date) + first 15 s of video,
then clipcut.features.extract -> data/shorts/<query>.jsonl.
Label ideas: like/view, comment/view, views / channel median views (controls for channel size). No retention curve is public;
YouTube Studio retention exists only for channels you own (Derek's/creator partners' own uploads via YouTube Analytics API).
Respect platform ToS; metadata-only mode (--no-video) avoids downloading media."""
import json, subprocess, sys, argparse
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent; YT = str(Path(sys.executable).parent / "yt-dlp")

def search(q, n):
    r = subprocess.run([YT, "--flat-playlist", "-J", f"ytsearch{n}:{q}"], capture_output=True, text=True, timeout=120)
    return [e["url"] for e in json.loads(r.stdout)["entries"]]

def meta(url):
    r = subprocess.run([YT, "-J", "--skip-download", url], capture_output=True, text=True, timeout=120)
    if r.returncode: raise RuntimeError(r.stderr[-300:])
    j = json.loads(r.stdout)
    return {k: j.get(k) for k in ["id", "title", "duration", "view_count", "like_count", "comment_count", "channel_id", "channel_follower_count", "upload_date", "width", "height"]}

def clip(url, out):
    subprocess.run([YT, "-f", "worst[ext=mp4]/worst", "--download-sections", "*0-15", "-o", str(out), url], capture_output=True, timeout=300)
    return out.exists()

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("query"); ap.add_argument("--n", type=int, default=20); ap.add_argument("--no-video", action="store_true")
    a = ap.parse_args(); d = ROOT / "data" / "shorts"; (d / "videos").mkdir(parents=True, exist_ok=True)
    out = open(d / (a.query.replace(" ", "_").replace("#", "") + ".jsonl"), "a")
    for u in search(a.query, a.n):
        try:
            m = meta(u)
            if m["duration"] and m["duration"] > 180: continue
            if not a.no_video:
                p = d / "videos" / f"{m['id']}.mp4"
                if clip(u, p):
                    from clipcut.features import extract
                    f = extract(p); f.pop("clip"); m["features"] = f
            out.write(json.dumps(m) + "\n"); out.flush(); print("ok", m["id"], m["view_count"], m["like_count"])
        except Exception as e: print("fail", u, str(e)[:120])
