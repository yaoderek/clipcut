"""Put a platform-ready mp4 into the bundle: link/copy when the variant already fits,
ffmpeg reframe (crop top/bottom for a moderately wider frame, blurred fill otherwise) and/or trim otherwise.
Never raises for a missing file or missing ffmpeg; returns a status instead."""
from __future__ import annotations

import os
import shutil
import subprocess

from .specs import ratio

RENDER_TIMEOUT_S = 600


def ffmpeg_available():
    return shutil.which("ffmpeg") is not None


def _link_or_copy(src, dst):
    if dst.exists():
        dst.unlink()
    try:
        os.link(src, dst)
    except OSError:
        shutil.copyfile(src, dst)


def reframe_filter(src_aspect, target_aspect, w, h):
    rs, rt = ratio(src_aspect), ratio(target_aspect)
    if abs(rs - rt) < 1e-3:
        return f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1"
    if rs < rt <= 2 * rs:  # moderately wider target (9:16 -> 4:5 / 1:1): keep full width, crop top/bottom around center
        return f"crop=iw:'trunc(iw/{rt:.6f}/2)*2',scale={w}:{h},setsar=1"
    # narrower target, or much wider: whole frame over a blurred fill
    return (f"split[a][b];[a]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},boxblur=20:2[bg];"
            f"[b]scale={w}:{h}:force_original_aspect_ratio=decrease[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1")


def prepare_video(src, dst, *, src_aspect, target_aspect, resolution, reframe, trim_to=None, render=True):
    """Returns dict(status, note, path|None). status: copied | reframed | trimmed | missing | skipped | unavailable | failed."""
    if src is None or not src.is_file():
        return dict(status="missing", path=None,
                    note="Variant mp4 is not on this server (mp4s are not in git). Text, captions, and copy are still in the bundle.")
    if not reframe and trim_to is None:
        _link_or_copy(src, dst)
        return dict(status="copied", path=dst, note=None)
    action = "reframe" if reframe else "trim"
    if not render:
        return dict(status="skipped", path=None, note=f"Needs a {action}; render was off for this export.")
    if not ffmpeg_available():
        return dict(status="unavailable", path=None, note=f"Needs a {action} but ffmpeg is not installed.")
    w, h = resolution
    cmd = ["ffmpeg", "-v", "error", "-y", "-i", str(src)]
    if trim_to is not None:
        cmd += ["-t", f"{trim_to:.3f}"]
    if reframe:
        cmd += ["-filter_complex" if "[" in reframe_filter(src_aspect or target_aspect, target_aspect, w, h) else "-vf",
                reframe_filter(src_aspect or target_aspect, target_aspect, w, h)]
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p"]
    else:
        cmd += ["-c:v", "copy"]
    cmd += ["-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(dst)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=RENDER_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return dict(status="failed", path=None, note=f"ffmpeg {action} failed: {exc}")
    if proc.returncode != 0 or not dst.is_file():
        tail = (proc.stderr or "").strip().splitlines()[-1:] or [f"exit {proc.returncode}"]
        return dict(status="failed", path=None, note=f"ffmpeg {action} failed: {tail[0][:200]}")
    return dict(status="reframed" if reframe else "trimmed", path=dst, note=None)
