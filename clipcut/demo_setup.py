"""Fetch public demo media and write simulated per-variant metrics.

Videos are downloaded, never committed. Network failures are recorded and do
not raise: a checkout with no route to the sample host still gets metrics
for runs whose manifests are already on disk.

Channel ids are ``CHANNEL_SPECS`` names lowercased, the same ids
``persona_api`` accepts (``tiktok``, ``shorts``, ``reels``, ``x``, ``linkedin``).
This repo has no ``yt_shorts`` or ``ig_reels`` id.
"""
from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

LIVE_ORIGIN = "https://clipcut-dubhacks.fly.dev"
PRIMARY_RUN = "v2_niosh_multi"
SAMPLE_NAME = "talking_head_niosh.mp4"

# Commons file is the full NIOSH interview. The checked-in run used seconds 28–148.
NIOSH_SOURCE = {
    "file": SAMPLE_NAME,
    "page": "https://commons.wikimedia.org/wiki/File:Science_Speaks_-_Talking_to_Women_in_Science_(Liz,_Epidemiologist).webm",
    "url": "https://upload.wikimedia.org/wikipedia/commons/7/7b/Science_Speaks_-_Talking_to_Women_in_Science_%28Liz%2C_Epidemiologist%29.webm",
    "license": "Public domain (PD US HHS CDC NIOSH)",
    "creator": "National Institute for Occupational Safety and Health",
    "start_s": 28.0,
    "duration_s": 120.053,
}

VERTICAL = ("tiktok", "shorts", "reels")
# Aspect → one channel id. 9:16 is handled separately so a run can fill all three.
ASPECT_CHANNEL = {"1:1": "x", "4:5": "linkedin", "16:9": "youtube"}
_MIN_VIDEO = 100_000
_MIN_POSTER = 400
_UA = "clipcut-demo-setup"


def repo_root():
    return Path(__file__).resolve().parent.parent


def channel_for(manifest, cursor=0):
    """Return ``(channel_id, next_cursor)``.

    9:16 uses the variant's own channel when that id is tiktok, shorts, or
    reels; otherwise it round-robins those three. 1:1 is x, 4:5 is linkedin.
    """
    if not isinstance(manifest, dict):
        manifest = {}
    recipe = manifest.get("recipe") if isinstance(manifest.get("recipe"), dict) else {}
    aspect = str(manifest.get("aspect") or recipe.get("aspect") or "9:16").strip()
    raw = str(manifest.get("channel") or recipe.get("channel") or "").strip().lower()
    if aspect == "9:16":
        if raw in VERTICAL:
            return raw, cursor
        return VERTICAL[cursor % 3], cursor + 1
    if aspect in ASPECT_CHANNEL:
        return ASPECT_CHANNEL[aspect], cursor
    if raw in VERTICAL or raw in ASPECT_CHANNEL.values() or raw in {"ig_feed", "ig_stories", "fb_reels", "fb_feed"}:
        return raw, cursor
    return VERTICAL[cursor % 3], cursor + 1


def media_missing(root=None, run=PRIMARY_RUN):
    """True when the sample mp4 or a preload variant mp4 is not on disk yet."""
    root = Path(root) if root is not None else repo_root()
    sample = root / "samples" / SAMPLE_NAME
    if not _big(sample, _MIN_VIDEO):
        return True
    directory = root / "runs" / run
    ids = _variant_ids(directory)
    if not ids:
        return True
    return any(not _big(directory / f"{vid}.mp4", _MIN_VIDEO) for vid in ids)


def setup(root=None, origin=LIVE_ORIGIN, runs=None, timeout=45):
    """Seed metrics, then download missing media. Always returns a summary."""
    root = Path(root) if root is not None else repo_root()
    runs = tuple(runs) if runs is not None else (PRIMARY_RUN,)
    summary = {"ok": True, "seeded": [], "present": [], "downloaded": [], "failed": [], "newest": None}
    for run in runs:
        try:
            seeded = seed_run(run, root)
        except Exception as exc:  # local JSON problem — keep going so one bad run doesn't block the rest
            summary["ok"] = False
            summary["failed"].append(f"{run} metrics: {exc}")
            continue
        if seeded:
            summary["seeded"].append(run)
    try:
        _fetch_samples(root, origin, timeout, summary)
    except Exception as exc:
        summary["failed"].append(f"samples: {exc}")
    for run in runs:
        try:
            _fetch_run_media(root, origin, run, timeout, summary)
        except Exception as exc:
            summary["failed"].append(f"{run} media: {exc}")
    primary = PRIMARY_RUN if PRIMARY_RUN in runs else (runs[0] if runs else None)
    if primary and (root / "runs" / primary).is_dir():
        mark_newest(root / "runs" / primary)
        summary["newest"] = primary
    return summary


def seed_run(run, root=None):
    """Write simulated metrics for one run. No-op when demo metrics are already there.

    Real (non-simulated) metrics.json is left untouched. Returns True when this
    call wrote the file.
    """
    root = Path(root) if root is not None else repo_root()
    directory = root / "runs" / run
    if not directory.is_dir():
        return False
    path = directory / "metrics.json"
    if path.is_file():
        try:
            existing = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            existing = None
        if isinstance(existing, dict):
            meta = existing.get("_meta") if isinstance(existing.get("_meta"), dict) else {}
            if meta.get("simulated") is True or meta.get("demo") is True:
                _write_demo_meta(directory)
                _stamp_demo_flags(path, existing)
                return False
            if any(isinstance(row, dict) and "views" in row for row in existing.values()):
                return False
    manifests = _load_manifests(directory)
    if len(manifests) < 1:
        return False
    _write_demo_meta(directory)
    payload = _metrics_payload(manifests)
    from clipcut.persona_api import store_metrics

    store_metrics(run=run, payload=payload, runs_root=root / "runs")
    stored = json.loads(path.read_text())
    _stamp_demo_flags(path, stored)
    return True


def mark_newest(directory):
    """Bump mtimes so a newest-run opener lands on this folder after setup."""
    directory = Path(directory)
    if not directory.is_dir():
        return
    now = None
    os.utime(directory, now)
    for name in ("results.json", "metrics.json", "manifest.json", "demo.json", "winner.json"):
        path = directory / name
        if path.is_file():
            os.utime(path, now)


def main(argv=None):
    try:
        summary = setup()
    except Exception as exc:
        print(f"[demo] setup failed: {exc}", file=sys.stderr)
        return 0
    downloaded = len(summary["downloaded"])
    present = len(summary["present"])
    failed = summary["failed"]
    print(f"[demo] seeded {summary['seeded'] or 'nothing new'}; media present {present}, downloaded {downloaded}.")
    if summary.get("newest"):
        print(f"[demo] newest run: {summary['newest']}")
    if failed:
        print(f"[demo] {len(failed)} fetch step(s) skipped (offline or missing on the host):", file=sys.stderr)
        for item in failed[:8]:
            print(f"[demo]   {item}", file=sys.stderr)
        if len(failed) > 8:
            print(f"[demo]   … {len(failed) - 8} more", file=sys.stderr)
    return 0


def _metrics_payload(manifests):
    from clipcut.persona_models import heuristic_variant

    rng = random.Random(20261018)
    cursor = 0
    metrics = {}
    for manifest in sorted(manifests, key=lambda item: str(item.get("variant_id") or "")):
        vid = str(manifest.get("variant_id") or "")
        if not vid:
            continue
        try:
            aggregate = float(heuristic_variant(manifest)["aggregate"])
        except Exception:
            aggregate = 0.5
        channel, cursor = channel_for(manifest, cursor)
        jitter = rng.uniform(-0.06, 0.06)
        watch = min(96.0, max(18.0, (aggregate + jitter) * 100.0))
        views = int(min(20000, max(400, round(900 + aggregate * 8000 + rng.randint(-250, 250)))))
        rate = min(0.08, max(0.004, 0.008 + aggregate * 0.045 + rng.uniform(-0.004, 0.004)))
        shares = min(views, max(1, int(round(views * rate))))
        likes = min(views, max(shares, int(round(shares * rng.uniform(2.2, 4.5)))))
        metrics[vid] = {
            "views": views,
            "avg_watch_pct": round(watch, 2),
            "shares": shares,
            "likes": likes,
            "channel": channel,
            "simulated": True,
        }
    return {
        "simulated": True,
        "metrics": metrics,
        "_meta": {
            "simulated": True,
            "demo": True,
            "generator": "clipcut.demo_setup simulated demo",
            "note": "Simulated from the hand-set persona aggregate plus noise. Not platform data.",
        },
    }


def _stamp_demo_flags(path, data):
    meta = data.get("_meta") if isinstance(data.get("_meta"), dict) else {}
    meta["demo"] = True
    meta["simulated"] = True
    meta.setdefault("note", "Simulated from the hand-set persona aggregate plus noise. Not platform data.")
    data["_meta"] = meta
    for key, row in list(data.items()):
        if key == "_meta" or not isinstance(row, dict) or "views" not in row:
            continue
        row["simulated"] = True
        row["demo"] = True
    path.write_text(json.dumps(data, indent=2) + "\n")


def _write_demo_meta(directory):
    path = directory / "demo.json"
    if path.is_file():
        return
    path.write_text(json.dumps({
        "demo": True,
        "simulated": True,
        "note": "Simulated demo metrics, not platform results.",
    }, indent=2) + "\n")


def _load_manifests(directory):
    out = []
    for path in sorted(directory.glob("v[0-9][0-9].json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("variant_id"):
            out.append(data)
    return out


def _variant_ids(directory):
    return [path.stem for path in sorted(directory.glob("v[0-9][0-9].json"))]


def _fetch_samples(root, origin, timeout, summary):
    samples = root / "samples"
    samples.mkdir(parents=True, exist_ok=True)
    names = _sample_names(origin, timeout)
    if SAMPLE_NAME not in names:
        names.append(SAMPLE_NAME)
    for name in names:
        dest = samples / Path(name).name
        if _big(dest, _MIN_VIDEO):
            summary["present"].append(dest.name)
            continue
        live = origin.rstrip("/") + "/samples/" + urllib.request.quote(dest.name)
        if _download(live, dest, timeout, minimum=_MIN_VIDEO):
            summary["downloaded"].append(dest.name)
            continue
        if dest.name == SAMPLE_NAME and _fetch_niosh_fallback(samples, timeout):
            summary["downloaded"].append(dest.name)
            continue
        summary["failed"].append(f"sample {dest.name}")


def _fetch_niosh_fallback(samples, timeout):
    """Trim the public-domain Commons interview when the live app does not serve /samples."""
    dest = samples / SAMPLE_NAME
    cache = samples / ".cache"
    cache.mkdir(parents=True, exist_ok=True)
    webm = cache / "niosh_liz_full.webm"
    if not _big(webm, _MIN_VIDEO):
        if not _download(NIOSH_SOURCE["url"], webm, timeout, minimum=_MIN_VIDEO):
            return False
    if shutil.which("ffmpeg") is None:
        return False
    tmp = _partial(dest)
    cmd = [
        "ffmpeg", "-v", "error", "-y",
        "-ss", str(NIOSH_SOURCE["start_s"]),
        "-t", str(NIOSH_SOURCE["duration_s"]),
        "-i", str(webm),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
        str(tmp),
    ]
    try:
        subprocess.run(cmd, check=True, timeout=max(timeout, 180))
    except (subprocess.SubprocessError, OSError):
        if tmp.exists():
            tmp.unlink()
        return False
    if not _big(tmp, _MIN_VIDEO):
        if tmp.exists():
            tmp.unlink()
        return False
    tmp.replace(dest)
    return True


def _fetch_run_media(root, origin, run, timeout, summary):
    directory = root / "runs" / run
    if not directory.is_dir():
        return
    base = origin.rstrip("/") + "/runs/" + urllib.request.quote(run)
    for vid in _variant_ids(directory):
        video = directory / f"{vid}.mp4"
        poster = directory / f".{vid}_poster.jpg"
        if _big(video, _MIN_VIDEO):
            summary["present"].append(f"{run}/{video.name}")
        elif _download(f"{base}/{vid}.mp4", video, timeout, minimum=_MIN_VIDEO):
            summary["downloaded"].append(f"{run}/{video.name}")
        else:
            summary["failed"].append(f"{run}/{video.name}")
        if _big(poster, _MIN_POSTER):
            summary["present"].append(f"{run}/{poster.name}")
        elif _download(f"{base}/{urllib.request.quote(poster.name)}", poster, timeout, minimum=_MIN_POSTER):
            summary["downloaded"].append(f"{run}/{poster.name}")
        elif _big(video, _MIN_VIDEO) and _poster_from_video(video, poster):
            summary["downloaded"].append(f"{run}/{poster.name}")
        else:
            summary["failed"].append(f"{run}/{poster.name}")
    winner = directory / "winner.mp4"
    if not _big(winner, _MIN_VIDEO):
        if _download(f"{base}/winner.mp4", winner, timeout, minimum=_MIN_VIDEO):
            summary["downloaded"].append(f"{run}/winner.mp4")


def _sample_names(origin, timeout):
    url = origin.rstrip("/") + "/api/samples"
    try:
        payload = _read_url(url, timeout)
    except (OSError, urllib.error.URLError, TimeoutError, ValueError):
        return []
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [str(item) for item in data if isinstance(item, str) and item.endswith(".mp4")]


def _download(url, dest, timeout, minimum):
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = _partial(dest)
    try:
        blob = _read_url(url, timeout)
    except (OSError, urllib.error.URLError, TimeoutError, ValueError):
        if tmp.exists():
            tmp.unlink()
        return False
    if len(blob) < minimum:
        return False
    tmp.write_bytes(blob)
    tmp.replace(dest)
    return True


def _read_url(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        status = getattr(resp, "status", 200)
        if status and int(status) >= 400:
            raise urllib.error.URLError(f"HTTP {status}")
        return resp.read()


def _poster_from_video(video, poster):
    if shutil.which("ffmpeg") is None:
        return False
    tmp = _partial(poster)
    cmd = ["ffmpeg", "-v", "error", "-y", "-ss", "1.5", "-i", str(video), "-frames:v", "1", "-vf", "scale=360:-2", str(tmp)]
    try:
        subprocess.run(cmd, check=True, timeout=60)
    except (subprocess.SubprocessError, OSError):
        if tmp.exists():
            tmp.unlink()
        return False
    if not _big(tmp, _MIN_POSTER):
        if tmp.exists():
            tmp.unlink()
        return False
    tmp.replace(poster)
    return True


def _partial(dest):
    """Temp path that keeps a real media extension so ffmpeg can open it."""
    dest = Path(dest)
    return dest.with_name(dest.stem + ".partial" + dest.suffix)


def _big(path, minimum):
    path = Path(path)
    try:
        return path.is_file() and path.stat().st_size >= minimum
    except OSError:
        return False
