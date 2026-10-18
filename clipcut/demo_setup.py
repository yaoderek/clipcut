"""Fetch public demo media and write simulated per-variant metrics.

Videos are downloaded, never committed. Network failures are recorded and do
not raise: a checkout with no route to the sample host still gets metrics
for runs whose manifests are already on disk.

Channel ids are ``CHANNEL_SPECS`` names lowercased, the same ids
``persona_api`` accepts (``tiktok``, ``shorts``, ``reels``, ``x``, ``linkedin``).
This repo has no ``yt_shorts`` or ``ig_reels`` id.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import time
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
    "cache": "niosh_liz_full.webm",
    "start_s": 28.0,
    "duration_s": 120.053,
}
# Short public-domain talking heads. Trimmed on download so a fresh clone stays small.
EXTRA_SOURCES = (
    {
        "file": "talking_head_kendra.mp4",
        "page": "https://commons.wikimedia.org/wiki/File:Science_Speaks_-_Talking_to_Women_in_Science_(Kendra,_Industrial_Hygienist).webm",
        "url": "https://upload.wikimedia.org/wikipedia/commons/e/ed/Science_Speaks_-_Talking_to_Women_in_Science_%28Kendra%2C_Industrial_Hygienist%29.webm",
        "license": "Public domain (PD US HHS CDC NIOSH)",
        "creator": "National Institute for Occupational Safety and Health",
        "cache": "niosh_kendra_full.webm",
        "start_s": 8.0,
        "duration_s": 45.0,
    },
    {
        "file": "talking_head_nasa_calvin.mp4",
        "page": "https://commons.wikimedia.org/wiki/File:NASA_%26_NOAA_Interview_Opportunity-_Snapshot_of_Earth_in_2024_Through_Temperature_Live_Shots_(SVS14742_-_KATE_CALVIN_2024_GLOBAL_TEMP_UPDATE).webm",
        "url": "https://upload.wikimedia.org/wikipedia/commons/7/73/NASA_%26_NOAA_Interview_Opportunity-_Snapshot_of_Earth_in_2024_Through_Temperature_Live_Shots_%28SVS14742_-_KATE_CALVIN_2024_GLOBAL_TEMP_UPDATE%29.webm",
        "license": "Public domain (NASA)",
        "creator": "NASA / NOAA",
        "cache": "nasa_calvin_full.webm",
        "start_s": 2.0,
        "duration_s": 45.0,
    },
)
FALLBACKS = {item["file"]: item for item in (NIOSH_SOURCE, *EXTRA_SOURCES)}
# JSON runs that reuse the NIOSH variant manifests. Cutter v2 is not run here.
EXTRA_RUNS = (
    {"run": "demo_pd_kendra", "sample": "talking_head_kendra.mp4"},
    {"run": "demo_pd_calvin", "sample": "talking_head_nasa_calvin.mp4"},
)

# cutter_v2 also appends the X and LinkedIn formats, so a run is a few more than this.
EXTRA_N = 8
RENDER_TIMEOUT_S = 1800
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
    """True when a demo sample or a preload variant mp4 is not on disk yet."""
    root = Path(root) if root is not None else repo_root()
    for name in FALLBACKS:
        if not _big(root / "samples" / name, _MIN_VIDEO):
            return True
    directory = root / "runs" / run
    ids = _variant_ids(directory)
    if not ids:
        return True
    if any(not _big(directory / f"{vid}.mp4", _MIN_VIDEO) for vid in ids):
        return True
    for spec in EXTRA_RUNS:
        if not _render_complete(root / "runs" / spec["run"]):
            return True
    return False


def run_is_visible(directory):
    """False while an extra demo run is still a NIOSH JSON clone.

    Those runs stay off ``/api/runs`` (and therefore the canvas) until a real
    cutter render writes ``.rendered`` and the manifest no longer says
    ``reused_from``.
    """
    directory = Path(directory)
    if directory.name not in {item["run"] for item in EXTRA_RUNS}:
        return True
    if not (directory / ".rendered").is_file():
        return False
    manifest_path = directory / "manifest.json"
    if not manifest_path.is_file():
        return False
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(manifest, dict) or manifest.get("reused_from"):
        return False
    return True


def setup(root=None, origin=LIVE_ORIGIN, runs=None, timeout=45, render=True, start_cut=None, variants=EXTRA_N):
    """Seed metrics, then download missing media. Always returns a summary.

    ``start_cut(run, cmd, n)`` claims the one-job lock and returns a started
    process. The CLI path leaves it None and starts cutter v2 itself.
    """
    root = Path(root) if root is not None else repo_root()
    runs = tuple(runs) if runs is not None else (PRIMARY_RUN,)
    summary = {"ok": True, "seeded": [], "present": [], "downloaded": [], "failed": [], "rendered": [], "newest": None}
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
    if render:
        try:
            render_extra_runs(root, summary, start_cut=start_cut, variants=variants)
        except Exception as exc:
            summary["ok"] = False
            summary["failed"].append(f"render: {exc}")
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
    payload = _metrics_payload(manifests, run)
    from clipcut.persona_api import store_metrics

    store_metrics(run=run, payload=payload, runs_root=root / "runs")
    stored = json.loads(path.read_text())
    _stamp_demo_flags(path, stored)
    return True


def mark_newest(directory):
    """Bump mtimes so a newest-run opener lands on this folder after setup.

    One second ahead of the clock, so a render that just finished cannot tie it.
    """
    directory = Path(directory)
    if not directory.is_dir():
        return
    stamp = time.time() + 1
    os.utime(directory, (stamp, stamp))
    for name in ("results.json", "metrics.json", "manifest.json", "demo.json", "winner.json"):
        path = directory / name
        if path.is_file():
            os.utime(path, (stamp, stamp))


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
    if summary.get("rendered"):
        print(f"[demo] rendered {', '.join(summary['rendered'])}")
    if summary.get("newest"):
        print(f"[demo] newest run: {summary['newest']}")
    if failed:
        print(f"[demo] {len(failed)} fetch step(s) skipped (offline or missing on the host):", file=sys.stderr)
        for item in failed[:8]:
            print(f"[demo]   {item}", file=sys.stderr)
        if len(failed) > 8:
            print(f"[demo]   … {len(failed) - 8} more", file=sys.stderr)
    return 0


def _rng_for(run):
    """Stable RNG for one run. Python's hash() is salted per process, so this is SHA-256."""
    digest = hashlib.sha256(str(run).encode("utf-8")).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def _metrics_payload(manifests, run):
    from clipcut.persona_models import heuristic_variant

    rng = _rng_for(run)
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


def _render_complete(directory):
    """True when cutter v2 has written real variant videos and a score."""
    directory = Path(directory)
    manifest_path = directory / "manifest.json"
    if not (directory / "results.json").is_file() or not manifest_path.is_file():
        return False
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(manifest, dict) or manifest.get("reused_from"):
        return False
    ids = _variant_ids(directory)
    if not ids:
        return False
    return all(_big(directory / f"{vid}.mp4", _MIN_VIDEO) for vid in ids)


def render_extra_runs(root, summary, start_cut=None, variants=EXTRA_N):
    """Cut, score, and seed each extra sample. Skip a run that already rendered."""
    root = Path(root)
    for spec in EXTRA_RUNS:
        _render_one(root, spec, summary, start_cut=start_cut, variants=variants)


def _render_one(root, spec, summary, start_cut, variants):
    run = spec["run"]
    dest = root / "runs" / run
    sample = root / "samples" / spec["sample"]
    if _render_complete(dest):
        _publish_render(dest, root)
        summary["present"].append(f"{run} render")
        return
    if not _big(sample, _MIN_VIDEO):
        summary["failed"].append(f"{run}: sample missing")
        return
    _clear_reuse_clone(dest)
    dest.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, str(root / "cli.py"), "cut2", str(sample),
        "--out", str(dest), "-n", str(variants),
    ]
    print(f"[demo] rendering {run} from {sample.name} ({variants} variants)", flush=True)
    log_path = dest / "job.log"
    started = time.time()
    if start_cut is not None:
        proc = start_cut(run, cmd, variants)
    else:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen(cmd, cwd=str(root), stdout=open(log_path, "w"), stderr=subprocess.STDOUT)
    if proc is None:
        summary["failed"].append(f"{run}: cutter busy")
        return
    code = _follow_cut(proc, log_path, run, RENDER_TIMEOUT_S)
    elapsed = time.time() - started
    if code != 0 or not _render_complete(dest):
        summary["failed"].append(f"{run}: cutter exit {code}")
        print(f"[demo] {run} render failed after {elapsed:.0f}s (exit {code})", flush=True)
        return
    _publish_render(dest, root)
    winner = _winner_of(dest)
    summary["rendered"].append(run)
    print(f"[demo] {run} rendered in {elapsed:.0f}s, winner {winner}", flush=True)


def _clear_reuse_clone(dest):
    """Drop a NIOSH JSON clone so cutter v2 does not score the wrong manifests."""
    if not dest.is_dir():
        return
    manifest_path = dest / "manifest.json"
    if not manifest_path.is_file():
        return
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError):
        return
    if isinstance(manifest, dict) and manifest.get("reused_from"):
        shutil.rmtree(dest)


def _publish_render(dest, root):
    """Simulated metrics, then the marker that lets /api/runs list the run."""
    if not _render_complete(dest):
        return
    seed_run(dest.name, root)
    if not (dest / "metrics.json").is_file():
        return
    (dest / ".rendered").write_text(json.dumps({
        "renderer": "cutter_v2",
        "demo": True,
        "simulated": True,
    }) + "\n")


def _winner_of(dest):
    try:
        data = json.loads((dest / "results.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None
    winner = data.get("winner") if isinstance(data, dict) else None
    return winner if isinstance(winner, str) else None


def _follow_cut(proc, log_path, run, timeout_s):
    """Print cutter progress lines until the process exits."""
    deadline = time.time() + timeout_s
    pos = 0
    while True:
        pos = _print_new_log(log_path, pos, run)
        if proc.poll() is not None:
            _print_new_log(log_path, pos, run)
            return proc.returncode
        if time.time() > deadline:
            proc.kill()
            proc.wait(timeout=30)
            return proc.returncode if proc.returncode is not None else -9
        time.sleep(0.4)


def _print_new_log(log_path, pos, run):
    if not log_path.is_file():
        return pos
    try:
        with open(log_path, encoding="utf-8", errors="replace") as handle:
            handle.seek(pos)
            chunk = handle.read()
            pos = handle.tell()
    except OSError:
        return pos
    for line in chunk.splitlines():
        if line.startswith(("[render]", "[v2]", "WINNER", "wrote", "v0", "v1")):
            print(f"[demo] {run} {line}", flush=True)
    return pos


def _fetch_samples(root, origin, timeout, summary):
    samples = root / "samples"
    samples.mkdir(parents=True, exist_ok=True)
    names = _sample_names(origin, timeout)
    for name in FALLBACKS:
        if name not in names:
            names.append(name)
    for name in names:
        dest = samples / Path(name).name
        if _big(dest, _MIN_VIDEO):
            summary["present"].append(dest.name)
            continue
        live = origin.rstrip("/") + "/samples/" + urllib.request.quote(dest.name)
        if _download(live, dest, timeout, minimum=_MIN_VIDEO):
            summary["downloaded"].append(dest.name)
            continue
        spec = FALLBACKS.get(dest.name)
        if spec is not None and _fetch_trimmed(samples, spec, timeout):
            summary["downloaded"].append(dest.name)
            continue
        summary["failed"].append(f"sample {dest.name}")


def _fetch_niosh_fallback(samples, timeout):
    """Trim the Liz interview when the live app does not serve /samples."""
    return _fetch_trimmed(samples, NIOSH_SOURCE, timeout)


def _fetch_trimmed(samples, spec, timeout):
    """Download a public-domain source and trim it to the demo sample."""
    dest = samples / spec["file"]
    cache = samples / ".cache"
    cache.mkdir(parents=True, exist_ok=True)
    webm = cache / spec["cache"]
    # Full interviews are tens of megabytes; allow the socket to stay open.
    if not _big(webm, _MIN_VIDEO):
        if not _download(spec["url"], webm, max(timeout, 180), minimum=_MIN_VIDEO):
            return False
    if shutil.which("ffmpeg") is None:
        return False
    tmp = _partial(dest)
    cmd = [
        "ffmpeg", "-v", "error", "-y",
        "-ss", str(spec["start_s"]),
        "-t", str(spec["duration_s"]),
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
