"""Drag-and-drop edit chips: one ffmpeg pass per chip, stacked by parent branch.

POST /api/branch queues a chip and returns immediately. A pool of three workers
renders a short low-res preview, then the full file. These jobs do not take the
one-at-a-time cut lock in app.JOBS.

See docs/BRANCH_API.md.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from clipcut.hook_rewrite import original_hook

WORKERS = 3
PREVIEW_MAX_S = 8.0
HOOK_TEMPLATE = "Wait — this is the part worth keeping."
MUSIC_LABEL = "ffmpeg-generated sine bed (placeholder, not a licensed track)"
CHIP_TYPES = ("captions", "zoom", "length", "hook", "crop", "music")
CAPTION_STYLES = ("bold", "minimal", "karaoke")
ASPECTS = ("9:16", "1:1", "16:9")
TRACKS = ("upbeat", "chill", "none")
LENGTHS = (15, 30, 45)

_RUN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,80}$")
_BRANCH_RE = re.compile(r"^c_[0-9a-f]{8}$")
_PARENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,40}$")

_CROP_FULL = {"9:16": (720, 1280), "1:1": (1080, 1080), "16:9": (1280, 720)}
_CROP_PREVIEW = {"9:16": (270, 480), "1:1": (360, 360), "16:9": (640, 360)}

_LOCK = threading.Lock()
_CV = threading.Condition(_LOCK)
_PENDING = []
_INFLIGHT = 0
_STARTED = False
_POOL = None


class ChipError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def create_branch(body, *, runs_root, root):
    """Validate, persist, and queue one or more chip branches. Does not render in the request.

    A single spec chip returns that branch. A canvas drop that lists several chips
    (for example len15, then zoom, then captions) queues a chain and returns the leaf.
    """
    runs_root = Path(runs_root)
    root = Path(root)
    plan = _plan_request(body, runs_root, root)
    parent_id = plan["parent_variant_id"]
    leaf = None
    for index, chip in enumerate(plan["chips"]):
        leaf = _create_one(
            run=plan["run"],
            parent_variant_id=parent_id,
            chip=chip,
            runs_root=runs_root,
            root=root,
            input_url=plan.get("input_url"),
            file_override=plan.get("file_override") if index == 0 else None,
        )
        parent_id = leaf["branch_id"]
    return _accepted(leaf)


def _create_one(run, parent_variant_id, chip, runs_root, root, input_url, file_override):
    run_dir = runs_root / run
    if not run_dir.is_dir():
        raise ChipError(f"no such run: {run}", 404)
    if file_override is not None:
        parent = {"kind": "file", "stack": [], "source_path": str(Path(file_override).resolve())}
    else:
        parent = _resolve_parent(parent_variant_id, run, runs_root, root)
    manifest = _context_manifest(parent, run, runs_root)
    chip, stack_entry, hook = _prepare_chip(chip, manifest)
    stack = list(parent.get("stack") or []) + [stack_entry]
    bid = _new_id(runs_root)
    outdir = run_dir / "branches" / bid
    outdir.mkdir(parents=True, exist_ok=False)
    preview_url = f"/runs/{run}/branches/{bid}/preview.mp4"
    state = {
        "branch_id": bid,
        "run": run,
        "parent_variant_id": parent_variant_id,
        "parent_kind": parent["kind"],
        "source_path": parent.get("source_path"),
        "input_url": input_url,
        "status": "queued",
        "chip": chip,
        "stack": stack,
        "hook": hook,
        "preview_url": preview_url,
        "video_url": f"/runs/{run}/branches/{bid}/video.mp4",
        "thumb_url": f"/runs/{run}/branches/{bid}/thumb.jpg",
        "thumb_ready": False,
        "score": make_score(stack, bid, _guess_duration(stack), _hook_text(stack)),
        "error": None,
        "claim": None,
        "timing": {},
        "music_placeholder": _music_placeholder(stack),
        "music_label": MUSIC_LABEL if _music_placeholder(stack) else None,
    }
    _write_state(state, runs_root)
    _write_index(bid, run, runs_root)
    _ensure_scheduler()
    _enqueue({"id": bid, "runs_root": str(runs_root), "root": str(root)}, "preview")
    return state


def _accepted(state):
    return {
        "branch_id": state["branch_id"],
        "id": state["branch_id"],
        "status_url": f"/api/branch/{state['branch_id']}",
        "preview_url": state["preview_url"],
        "status": "queued",
        "run": state["run"],
        "video": state.get("input_url"),
        "source": (state.get("score") or {}).get("source"),
    }


def get_branch(branch_id, *, runs_root):
    if not isinstance(branch_id, str) or not _BRANCH_RE.fullmatch(branch_id):
        raise ChipError("no such branch", 404)
    state = _read_id(branch_id, Path(runs_root))
    if state is None:
        raise ChipError("no such branch", 404)
    return public_view(state)


def public_view(state):
    status = state.get("status") or "queued"
    view = {
        "branch_id": state["branch_id"],
        "run": state["run"],
        "parent_variant_id": state["parent_variant_id"],
        "parent_kind": state.get("parent_kind"),
        "status": status,
        "preview_url": state.get("preview_url"),
        "video_url": state.get("video_url") if status == "done" else None,
        "thumb_url": state.get("thumb_url") if state.get("thumb_ready") else None,
        "score": state.get("score"),
        "chip": state.get("chip"),
        "stack": state.get("stack") or [],
        "error": state.get("error"),
    }
    if status == "done":
        playable = state.get("video_url")
    elif status == "preview":
        playable = state.get("preview_url")
    else:
        playable = state.get("input_url")
    view["id"] = state["branch_id"]
    view["video"] = playable
    view["source"] = (state.get("score") or {}).get("source")
    if state.get("thumb_ready"):
        view["poster"] = state.get("thumb_url")
        view["thumbnail"] = state.get("thumb_url")
    if status in ("preview", "done"):
        view["preview_video"] = state.get("preview_url")
    if state.get("hook"):
        view["hook"] = state["hook"]
    if state.get("music_placeholder"):
        view["music_placeholder"] = True
        view["music_label"] = state.get("music_label") or MUSIC_LABEL
    if state.get("timing"):
        view["timing"] = state["timing"]
    return view


def make_score(stack, branch_id, duration, hook_text):
    """Persona heuristic when it returns quickly, otherwise a chip-weight simulation."""
    try:
        from clipcut.persona_api import score_variants

        manifest = score_manifest(stack, branch_id, duration, hook_text)
        scored = score_variants(manifests=[manifest], mode="heuristic", calibration="none")
        value = scored["variants"][0]["aggregate"]
        return {
            "value": value,
            "source": "heuristic",
            "disclaimer": "Heuristic persona score from the chip stack. Not a measured result.",
        }
    except Exception:
        return {
            "value": simulated_score(stack),
            "source": "simulated",
            "disclaimer": "Simulated chip score. Not a measured result.",
        }


def simulated_score(stack):
    score = 0.52
    for chip in stack or []:
        typ = chip.get("type")
        params = chip.get("params") or {}
        if typ == "captions":
            score += {"bold": 0.07, "karaoke": 0.09, "minimal": 0.03}.get(params.get("style"), 0.03)
        elif typ == "zoom" and params.get("punch_ins", True):
            try:
                intensity = float(params.get("intensity", 1))
            except (TypeError, ValueError):
                intensity = 1.0
            score += 0.05 * min(max(intensity, 0.0), 2.0)
        elif typ == "length":
            score += {15: 0.06, 30: 0.05, 45: 0.02}.get(int(params.get("seconds") or 0), 0.0)
        elif typ == "hook":
            score += 0.08 if (params.get("text") or chip.get("text")) else 0.04
        elif typ == "crop":
            score += {"9:16": 0.07, "1:1": 0.04, "16:9": 0.02}.get(params.get("aspect"), 0.02)
        elif typ == "music":
            score += {"upbeat": 0.04, "chill": 0.03, "none": 0.0}.get(params.get("track"), 0.0)
    return round(min(1.0, max(0.0, score)), 4)


def score_manifest(stack, branch_id, duration, hook_text):
    aspect = "9:16"
    captions_style = "none"
    hook_title = False
    cues = []
    cuts = []
    dur = float(duration or 30)
    for chip in stack or []:
        typ = chip.get("type")
        params = chip.get("params") or {}
        if typ == "crop" and params.get("aspect") in ASPECTS:
            aspect = params["aspect"]
        elif typ == "length":
            dur = min(dur, float(params.get("seconds") or dur))
        elif typ == "captions":
            captions_style = {"bold": "word_pop", "minimal": "minimal", "karaoke": "karaoke"}.get(params.get("style"), "sentence")
            line = (chip.get("text") or "CAPTIONS")[:48]
            cues = [{"start": 0.0, "end": min(2.0, dur), "text": line}]
        elif typ == "hook":
            hook_title = True
            hook_text = chip.get("text") or params.get("text") or hook_text
        elif typ == "zoom" and params.get("punch_ins", True):
            cuts = [round(t, 2) for t in (2.2, 4.4, 6.6, 8.8) if t < dur]
    channel = "TikTok" if aspect == "9:16" else ("X" if aspect == "1:1" else "YouTube")
    return {
        "variant_id": branch_id,
        "duration_s": round(dur, 2),
        "aspect": aspect,
        "channel": channel,
        "hook_end_s": 2.2 if hook_title else min(4.0, dur),
        "hook_text": hook_text or "Here is the cut",
        "hook_boundary": "pause",
        "cut_times_s": cuts,
        "captions": cues,
        "silence_removed_s": 0.4 if cuts else 0.0,
        "loop_end": True,
        "recipe": {
            "captions": captions_style,
            "ending": "loop",
            "hook_title": hook_title,
            "channel": channel,
            "aspect": aspect,
        },
    }


def resolve_hook(params, manifest):
    """Opening line for a hook chip. Request text, else persona hooks, else a template."""
    raw = (params or {}).get("text")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()[:140], "request"
    if not original_hook(manifest or {}):
        return HOOK_TEMPLATE, "template"
    try:
        from clipcut.persona_api import rewrite_hooks

        out = rewrite_hooks(manifests=[manifest], personas=["fast_swiper"], n=1, mode="heuristic")
        for row in (out.get("variants") or [{}])[0].get("suggestions") or []:
            text = (row.get("hook") or "").strip()
            if text:
                return text[:140], "persona-hooks"
    except Exception:
        pass
    return HOOK_TEMPLATE, "template"


def commands_for(state, info, stage, outdir):
    """FFmpeg argument lists for one stage. Writes the ASS file when the chip burns text.

    Each step is {args, fallback, timeout}. args do not include the ffmpeg binary.
    The last argument is the output path. state['_src'] is the input video.
    """
    chip = state["chip"]
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    dest = _stage_dest(outdir, stage)
    wh = target_wh(chip, info, stage)
    dur = output_duration(chip, info, stage)
    ass_path = None
    if chip["type"] in ("captions", "hook"):
        ass_path = outdir / ("captions.ass" if chip["type"] == "captions" else "hook.ass")
        text = _overlay_text(state)
        write_ass(ass_path, kind=chip["type"], text=text, duration=dur or min(info["duration"], 2.8), size=wh, style=(chip.get("params") or {}).get("style"))
    steps = []
    bed = None
    track = (chip.get("params") or {}).get("track")
    if chip["type"] == "music" and track not in (None, "none") and stage != "thumb":
        bed = outdir / "bed.m4a"
        if not bed.is_file():
            full_dur = output_duration(chip, info, "full") or info["duration"]
            steps.append({"args": bed_args(bed, track, full_dur), "fallback": None, "timeout": 30})
    steps.append(_video_step(state, info, stage, dest, wh, dur, ass_path, bed))
    return steps


def video_args(src, dest, chip, info, stage, wh, dur, ass_path, bed):
    """Argument list for the video (or still) output. Last item is dest."""
    src = str(src)
    dest = str(dest)
    typ = chip["type"]
    params = chip.get("params") or {}
    if stage == "thumb":
        ss = "0" if info["duration"] < 0.45 else "0.25"
        vf = _visual_filter(chip, wh, ass_path, preview=True)
        return ["-ss", ss, "-i", src, "-frames:v", "1", "-vf", vf, dest]
    if typ == "length" and stage == "full":
        return ["-i", src, "-t", f"{dur:.3f}", "-c", "copy", "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", dest]
    if typ == "music" and params.get("track") == "none" and stage == "full":
        return ["-i", src, "-c", "copy", "-movflags", "+faststart", dest]
    if typ == "music" and params.get("track") != "none" and stage == "full" and bed is not None:
        mix = _mix_args(src, bed, dest, info["has_audio"], limit=None, wh=None)
        return mix
    if typ == "music" and params.get("track") != "none" and bed is not None:
        return _mix_args(src, bed, dest, info["has_audio"], limit=dur, wh=wh)
    vf = _visual_filter(chip, wh, ass_path, preview=(stage != "full"))
    preset = "ultrafast" if stage != "full" else "veryfast"
    crf = "34" if stage != "full" else "22"
    fps = "12" if stage != "full" else "30"
    args = ["-i", src]
    if dur:
        args += ["-t", f"{dur:.3f}"]
    args += ["-vf", vf, "-c:v", "libx264", "-preset", preset, "-crf", crf, "-pix_fmt", "yuv420p", "-r", fps, "-threads", "2"]
    if info["has_audio"]:
        args += ["-c:a", "aac", "-b:a", "96k"]
    else:
        args += ["-an"]
    args += ["-movflags", "+faststart", dest]
    return args


def bed_args(dest, kind, duration):
    dur = max(0.5, min(float(duration), 180.0))
    if kind == "chill":
        fade = max(dur - 0.45, 0.0)
        lavfi = (
            f"sine=frequency=174:sample_rate=44100:duration={dur:.2f},"
            f"volume=0.4,afade=t=in:st=0:d=0.25,afade=t=out:st={fade:.2f}:d=0.35"
        )
    else:
        lavfi = (
            "aevalsrc="
            "0.2*sin(2*PI*349.23*t)*(0.55+0.45*sin(2*PI*3*t))"
            "+0.1*sin(2*PI*523.25*t)*(0.5+0.5*sin(2*PI*6*t))"
            f":s=44100:d={dur:.2f}"
        )
    return ["-f", "lavfi", "-i", lavfi, "-c:a", "aac", "-b:a", "96k", str(dest)]


def target_wh(chip, info, stage):
    if chip["type"] == "crop":
        aspect = (chip.get("params") or {}).get("aspect") or "9:16"
        table = _CROP_PREVIEW if stage != "full" else _CROP_FULL
        return table[aspect]
    return _fit(info["width"], info["height"], 480 if stage != "full" else 1280)


def output_duration(chip, info, stage):
    if stage == "thumb":
        return None
    dur = float(info["duration"])
    if chip["type"] == "length":
        dur = min(dur, float(chip["params"]["seconds"]))
    if stage == "preview":
        dur = min(dur, PREVIEW_MAX_S)
    return dur


def write_ass(path, *, kind, text, duration, size, style):
    width, height = size
    words = [w for w in str(text or "This is the cut").split() if w][:8] or ["Cut"]
    span = min(max(float(duration or 2.8), 0.8), 6.0)
    if kind == "hook":
        body = _ass_escape(" ".join(words))
        end = min(2.8, span)
        style_line = (
            f"Style: Hook,DejaVu Sans,{max(18, int(height * 0.055))},"
            "&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,3,0,8,"
            f"16,16,{max(18, int(height * 0.08))},1"
        )
        events = f"Dialogue: 0,{_ass_time(0)},{_ass_time(end)},Hook,,0,0,0,,{body}\n"
        style_name_block = style_line
    elif style == "karaoke":
        each = span / len(words)
        parts = []
        for word in words:
            parts.append(f"{{\\kf{max(1, int(round(each * 100)))}}}{_ass_escape(word)}")
        body = " ".join(parts)
        style_name_block = (
            f"Style: Karaoke,DejaVu Sans,{max(18, int(height * 0.06))},"
            "&H00FFFFFF,&H0000FFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,3,0,2,"
            f"16,16,{max(18, int(height * 0.08))},1"
        )
        events = f"Dialogue: 0,{_ass_time(0)},{_ass_time(span)},Karaoke,,0,0,0,,{body}\n"
    else:
        body = _ass_escape(" ".join(words))
        bold = style != "minimal"
        size_px = max(16, int(height * (0.055 if bold else 0.032)))
        colour = "&H00FFFFFF" if bold else "&HA0FFFFFF"
        weight = -1 if bold else 0
        outline = 3 if bold else 1
        style_name_block = (
            f"Style: Cap,DejaVu Sans,{size_px},{colour},&H000000FF,&H00000000,&H64000000,"
            f"{weight},0,0,0,100,100,0,0,1,{outline},0,2,16,16,{max(16, int(height * 0.07))},1"
        )
        events = f"Dialogue: 0,{_ass_time(0)},{_ass_time(span)},Cap,,0,0,0,,{body}\n"
    path.write_text(
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {width}\nPlayResY: {height}\nWrapStyle: 0\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"{style_name_block}\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        f"{events}"
    )


def probe_media(path):
    path = Path(path)
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "ffprobe failed")[-500:])
    data = json.loads(proc.stdout or "{}")
    duration = float((data.get("format") or {}).get("duration") or 0)
    width = height = 0
    has_audio = False
    for stream in data.get("streams") or []:
        if stream.get("codec_type") == "video" and not width:
            width = int(stream.get("width") or 0)
            height = int(stream.get("height") or 0)
        elif stream.get("codec_type") == "audio":
            has_audio = True
    if duration <= 0 or width <= 0 or height <= 0:
        raise RuntimeError(f"could not read video: {path}")
    return {"duration": duration, "width": width, "height": height, "has_audio": has_audio}


def ffmpeg_run(args, timeout=120):
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "ffmpeg failed")[-1500:])


def run_stage(job):
    """Render one stage (preview or full). Called by the worker pool."""
    try:
        _run_stage_body(job)
    except Exception as exc:
        _fail_job(job, exc)


def wait_idle(timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _CV:
            if _INFLIGHT == 0 and not _PENDING:
                return
        time.sleep(0.02)
    raise TimeoutError("chip workers still running")


def reset_for_tests():
    with _CV:
        _PENDING.clear()
        _CV.notify_all()


def _run_stage_body(job):
    runs_root = Path(job["runs_root"])
    stage = job["stage"]
    state = _read_id(job["id"], runs_root)
    if state is None or state.get("status") in ("error", "done"):
        return
    if stage == "full" and state.get("status") != "preview":
        return
    started = time.perf_counter()
    src = _input_media(state, stage, runs_root)
    info = probe_media(src)
    outdir = _branch_dir(state, runs_root)
    state["_src"] = str(src)
    if stage == "preview":
        for step in commands_for(state, info, "thumb", outdir):
            _emit(step)
        state["thumb_ready"] = True
        _write_state(state, runs_root)
        for step in commands_for(state, info, "preview", outdir):
            _emit(step)
        state.pop("_src", None)
        state["status"] = "preview"
        state["timing"] = dict(state.get("timing") or {})
        state["timing"]["preview_s"] = round(time.perf_counter() - started, 3)
        state["score"] = make_score(state["stack"], state["branch_id"], output_duration(state["chip"], info, "full"), _hook_text(state["stack"]))
        _write_state(state, runs_root)
        _poke()
        print(f"[chip] {state['branch_id']} preview {state['timing']['preview_s']:.2f}s", flush=True)
        return
    for step in commands_for(state, info, "full", outdir):
        _emit(step)
    state.pop("_src", None)
    state["status"] = "done"
    state["timing"] = dict(state.get("timing") or {})
    state["timing"]["full_s"] = round(time.perf_counter() - started, 3)
    state["score"] = make_score(state["stack"], state["branch_id"], output_duration(state["chip"], info, "full"), _hook_text(state["stack"]))
    _write_state(state, runs_root)
    _poke()
    print(f"[chip] {state['branch_id']} full {state['timing']['full_s']:.2f}s", flush=True)


def _video_step(state, info, stage, dest, wh, dur, ass_path, bed):
    src = state.get("_src")
    chip = state["chip"]
    params = chip.get("params") or {}
    args = video_args(src, dest, chip, info, stage, wh, dur, ass_path, bed)
    fallback = None
    if "copy" in args and chip["type"] == "music" and bed is not None and params.get("track") not in (None, "none"):
        fallback = _mix_args(src, bed, dest, info["has_audio"], limit=dur, wh=wh)
    elif "copy" in args:
        fallback = _reencode_args(src, dest, chip, info, stage, wh, dur, ass_path)
    timeout = 20 if stage == "thumb" else (60 if stage != "full" else 180)
    return {"args": args, "fallback": fallback, "timeout": timeout}


def _reencode_args(src, dest, chip, info, stage, wh, dur, ass_path):
    """Used when stream copy fails. Still applies a length trim."""
    vf = _visual_filter(chip if chip["type"] != "music" else {"type": "length", "params": {}}, wh, ass_path, preview=(stage != "full"))
    if chip["type"] == "music":
        vf = f"scale={wh[0]}:{wh[1]}:flags=fast_bilinear,setsar=1"
    args = ["-i", str(src)]
    if dur:
        args += ["-t", f"{dur:.3f}"]
    args += ["-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p", "-threads", "2"]
    if info["has_audio"]:
        args += ["-c:a", "aac", "-b:a", "96k"]
    else:
        args += ["-an"]
    args += ["-movflags", "+faststart", str(dest)]
    return args


def _emit(step):
    _emit_args(step["args"], step.get("timeout") or 120, step.get("fallback"), step.get("timeout") or 120)


def _emit_args(args, timeout, fallback, fallback_timeout):
    try:
        _write_part(args, timeout)
    except Exception:
        if not fallback:
            raise
        _write_part(fallback, fallback_timeout)


def _write_part(args, timeout):
    dest = Path(args[-1])
    part = dest.with_name(dest.stem + ".part" + dest.suffix)
    if part.exists():
        part.unlink()
    try:
        ffmpeg_run([*args[:-1], str(part)], timeout=timeout)
        if not part.is_file() or part.stat().st_size == 0:
            raise RuntimeError(f"empty ffmpeg output for {dest.name}")
        os.replace(part, dest)
    except Exception:
        if part.exists():
            part.unlink()
        raise


def _visual_filter(chip, wh, ass_path, preview):
    width, height = wh
    typ = chip["type"]
    params = chip.get("params") or {}
    if typ == "crop":
        base = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1"
    elif typ == "zoom" and params.get("punch_ins", True):
        extra = 0.18 * _intensity(params)
        if extra <= 0.001:
            base = f"scale={width}:{height}:flags=fast_bilinear,setsar=1"
        else:
            zoom = f"1+{extra:.4f}*lt(mod(t\\,2.6)\\,1.05)"
            base = (
                f"crop=w='trunc(in_w/({zoom})/2)*2':h='trunc(in_h/({zoom})/2)*2':"
                f"x='(in_w-out_w)/2':y='(in_h-out_h)/2',"
                f"scale={width}:{height}:flags=fast_bilinear,setsar=1"
            )
    else:
        base = f"scale={width}:{height}:flags=fast_bilinear,setsar=1"
    if ass_path is not None and typ in ("captions", "hook", "zoom", "crop", "length"):
        if typ in ("captions", "hook"):
            base = base + "," + _ass_filter(ass_path)
    return base


def _mix_args(src, bed, dest, has_audio, limit, wh):
    args = ["-i", str(src), "-i", str(bed)]
    if wh is None:
        if has_audio:
            graph = "[1:a]volume=0.22[b];[0:a][b]amix=inputs=2:duration=first:dropout_transition=0[a]"
            args += ["-filter_complex", graph, "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "128k"]
        else:
            args += ["-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-shortest"]
    else:
        width, height = wh
        if has_audio:
            graph = (
                f"[0:v]scale={width}:{height}:flags=fast_bilinear,setsar=1[v];"
                "[1:a]volume=0.22[b];[0:a][b]amix=inputs=2:duration=first:dropout_transition=0[a]"
            )
        else:
            graph = f"[0:v]scale={width}:{height}:flags=fast_bilinear,setsar=1[v];[1:a]volume=0.22[a]"
        args += ["-filter_complex", graph, "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "34", "-pix_fmt", "yuv420p", "-r", "12", "-threads", "2", "-c:a", "aac", "-b:a", "96k"]
    if limit:
        args += ["-t", f"{float(limit):.3f}"]
    args += ["-movflags", "+faststart", str(dest)]
    return args


def _ass_filter(path):
    escaped = str(Path(path)).replace("\\", "\\\\").replace(":", "\\:").replace("'", r"\'")
    return f"ass='{escaped}'"


def _ass_escape(text):
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}").replace("\n", " ")


def _ass_time(seconds):
    cs = max(0, int(round(float(seconds) * 100)))
    hours, rem = divmod(cs, 360000)
    minutes, rem = divmod(rem, 6000)
    secs, centis = divmod(rem, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"


def _intensity(params):
    try:
        value = float(params.get("intensity", 1))
    except (TypeError, ValueError):
        value = 1.0
    return min(max(value, 0.0), 3.0)


def _fit(width, height, max_side):
    scale = min(1.0, float(max_side) / float(max(width, height)))
    return _even(width * scale), _even(height * scale)


def _even(value):
    number = int(round(value))
    number -= number % 2
    return max(2, number)


def _stage_dest(outdir, stage):
    if stage == "thumb":
        return outdir / "thumb.jpg"
    if stage == "preview":
        return outdir / "preview.mp4"
    return outdir / "video.mp4"


_CANVAS_CHIPS = {
    "captions": ("captions", {"style": "bold"}),
    "zoom": ("zoom", {"punch_ins": True, "intensity": 1}),
    "len15": ("length", {"seconds": 15}),
    "len30": ("length", {"seconds": 30}),
    "len45": ("length", {"seconds": 45}),
    "length": ("length", {"seconds": 30}),
    "hook": ("hook", {}),
    "crop": ("crop", {"aspect": "9:16"}),
    "music": ("music", {"track": "upbeat"}),
}


def _require_run(body):
    if not isinstance(body, dict):
        raise ChipError("expected an object")
    run = body.get("run")
    if not isinstance(run, str) or not run:
        raise ChipError("run is required")
    if not _RUN_RE.fullmatch(run):
        raise ChipError("invalid run name")
    return run


def _plan_request(body, runs_root, root):
    """Spec body, or the canvas drop body (chips, variant_id, video, settings)."""
    run = _require_run(body)
    if isinstance(body.get("chip"), dict):
        parent = body.get("parent_variant_id")
        if parent is None or parent == "":
            raise ChipError("parent_variant_id is required")
        if not isinstance(parent, str):
            raise ChipError("parent_variant_id must be a string")
        if parent != "source" and not _PARENT_RE.fullmatch(parent) and not _BRANCH_RE.fullmatch(parent):
            raise ChipError("invalid parent_variant_id")
        chip = _normalize_chip(body["chip"])
        video = body.get("video") if isinstance(body.get("video"), str) else None
        input_url = video if video and video.startswith("/") else None
        if input_url is None and parent not in ("source",) and not _BRANCH_RE.fullmatch(parent):
            input_url = f"/runs/{run}/{parent}.mp4"
        return {"run": run, "parent_variant_id": parent, "chips": [chip], "input_url": input_url, "file_override": None}
    chips = _canvas_chip_list(body)
    video = body.get("video") if isinstance(body.get("video"), str) else None
    located = _locate_video(video, run, runs_root, root) if video else None
    if located and located.get("branch_id"):
        parent_state = _read_id(located["branch_id"], runs_root)
        stack_len = len((parent_state or {}).get("stack") or [])
        tail = chips[stack_len:] if len(chips) > stack_len else chips[-1:]
        return {
            "run": run,
            "parent_variant_id": located["branch_id"],
            "chips": tail,
            "input_url": located.get("input_url") or video,
            "file_override": None,
        }
    variant = body.get("variant_id") or body.get("parent_variant_id")
    if isinstance(variant, str) and variant and variant not in ("source", "null") and (runs_root / run / f"{variant}.mp4").is_file():
        return {
            "run": run,
            "parent_variant_id": variant,
            "chips": chips,
            "input_url": video if video and video.startswith("/") else f"/runs/{run}/{variant}.mp4",
            "file_override": None,
        }
    if located and located.get("file"):
        return {
            "run": run,
            "parent_variant_id": "source",
            "chips": chips,
            "input_url": located.get("input_url") or video,
            "file_override": located["file"],
        }
    return {"run": run, "parent_variant_id": "source", "chips": chips, "input_url": video if video and video.startswith("/") else None, "file_override": None}


def _canvas_chip_list(body):
    raw = body.get("chips")
    if not isinstance(raw, list) or not raw:
        raise ChipError("chip is required")
    settings = body.get("settings") if isinstance(body.get("settings"), dict) else {}
    out = []
    for item in raw:
        if not isinstance(item, str):
            raise ChipError("chips must be a list of chip names")
        name = item.strip().lower()
        if name in ("", "approaches"):
            continue
        if name not in _CANVAS_CHIPS:
            raise ChipError(f"unknown chip {item}")
        typ, defaults = _CANVAS_CHIPS[name]
        params = dict(defaults)
        if name == "crop" and settings.get("aspect") in ASPECTS:
            params["aspect"] = settings["aspect"]
        if name == "captions":
            params["style"] = {"word_pop": "bold", "bold": "bold", "sentence": "minimal", "minimal": "minimal", "karaoke": "karaoke"}.get(settings.get("captions"), "bold")
        if name == "music" and settings.get("track") in TRACKS:
            params["track"] = settings["track"]
        if name in ("length", "len15", "len30", "len45"):
            seconds = settings.get("length")
            if name == "length" and seconds in (15, 30, 45, "15", "30", "45"):
                params["seconds"] = int(seconds)
        if name == "zoom":
            intensity = settings.get("intensity")
            if isinstance(intensity, (int, float)) and not isinstance(intensity, bool) and 0 <= float(intensity) <= 3:
                params["intensity"] = float(intensity)
            if settings.get("punch_ins") is False:
                params["punch_ins"] = False
        out.append(_normalize_chip({"type": typ, "params": params}))
    if not out:
        raise ChipError("chip is required")
    if len(out) > 8:
        raise ChipError("at most 8 chips in one drop")
    return out


def _locate_video(url, run, runs_root, root):
    path = url.split("?", 1)[0].strip()
    if not path:
        return None
    file = None
    if path.startswith("/runs/"):
        file = runs_root / path[len("/runs/"):]
    elif path.startswith("/samples/"):
        file = root / "samples" / Path(path).name
    else:
        candidate = Path(path)
        if candidate.is_file():
            file = candidate
    if file is None or not file.is_file():
        return None
    file = file.resolve()
    match = re.search(r"/branches/(c_[0-9a-f]{8})(?:/|$)", path)
    if match and _read_id(match.group(1), runs_root):
        return {"branch_id": match.group(1), "file": file, "input_url": path if path.startswith("/") else None}
    input_url = path if path.startswith("/") else None
    return {"branch_id": None, "file": file, "input_url": input_url}


def _parse_body(body):
    if not isinstance(body, dict):
        raise ChipError("expected an object")
    run = body.get("run")
    if not isinstance(run, str) or not run:
        raise ChipError("run is required")
    if not _RUN_RE.fullmatch(run):
        raise ChipError("invalid run name")
    parent = body.get("parent_variant_id")
    if parent is None or parent == "":
        raise ChipError("parent_variant_id is required")
    if not isinstance(parent, str):
        raise ChipError("parent_variant_id must be a string")
    if parent != "source" and not _PARENT_RE.fullmatch(parent) and not _BRANCH_RE.fullmatch(parent):
        raise ChipError("invalid parent_variant_id")
    chip = _normalize_chip(body.get("chip"))
    return {"run": run, "parent_variant_id": parent, "chip": chip}


def _normalize_chip(chip):
    if not isinstance(chip, dict):
        raise ChipError("chip must be an object")
    typ = chip.get("type")
    if typ not in CHIP_TYPES:
        raise ChipError("chip.type must be captions, zoom, length, hook, crop, or music")
    params = chip.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ChipError("chip.params must be an object")
    if typ == "captions":
        style = params.get("style", "bold")
        if style not in CAPTION_STYLES:
            raise ChipError("captions.style must be bold, minimal, or karaoke")
        return {"type": typ, "params": {"style": style}}
    if typ == "zoom":
        punch = params.get("punch_ins", True)
        if not isinstance(punch, bool):
            raise ChipError("zoom.punch_ins must be a boolean")
        if "intensity" in params:
            intensity = params.get("intensity")
            if isinstance(intensity, bool) or not isinstance(intensity, (int, float)):
                raise ChipError("zoom.intensity must be a number")
            intensity = float(intensity)
            if intensity < 0 or intensity > 3:
                raise ChipError("zoom.intensity must be between 0 and 3")
        else:
            intensity = 1.0
        return {"type": typ, "params": {"punch_ins": punch, "intensity": round(intensity, 3)}}
    if typ == "length":
        seconds = params.get("seconds", 30)
        if isinstance(seconds, bool) or seconds not in (15, 30, 45, "15", "30", "45"):
            raise ChipError("length.seconds must be 15, 30, or 45")
        return {"type": typ, "params": {"seconds": int(seconds)}}
    if typ == "hook":
        text = params.get("text")
        if text is not None and not isinstance(text, str):
            raise ChipError("hook.text must be a string")
        out = {}
        if isinstance(text, str) and text.strip():
            out["text"] = text.strip()[:140]
        return {"type": typ, "params": out}
    if typ == "crop":
        aspect = params.get("aspect", "9:16")
        if aspect not in ASPECTS:
            raise ChipError("crop.aspect must be 9:16, 1:1, or 16:9")
        return {"type": typ, "params": {"aspect": aspect}}
    track = params.get("track", "upbeat")
    if track not in TRACKS:
        raise ChipError("music.track must be upbeat, chill, or none")
    return {"type": typ, "params": {"track": track}}


def _prepare_chip(chip, manifest):
    entry = {"type": chip["type"], "params": dict(chip["params"])}
    hook = None
    if chip["type"] == "hook":
        text, source = resolve_hook(chip["params"], manifest)
        entry["text"] = text
        entry["text_source"] = source
        hook = {"text": text, "source": source}
    elif chip["type"] == "captions":
        entry["text"] = _caption_line(manifest)
    return chip, entry, hook


def _caption_line(manifest):
    words = []
    caps = manifest.get("captions") if isinstance(manifest, dict) else None
    if isinstance(caps, list):
        for cue in caps[:8]:
            if isinstance(cue, dict) and isinstance(cue.get("text"), str):
                words.extend(cue["text"].split())
    if words:
        return " ".join(words[:8])
    line = original_hook(manifest or {})
    if line:
        return " ".join(line.split()[:8])
    return "This is the cut"


def _resolve_parent(parent_id, run, runs_root, root):
    if parent_id == "source":
        return {"kind": "source", "stack": [], "source_path": str(_find_source(run, runs_root, root)), "manifest": None}
    if _BRANCH_RE.fullmatch(parent_id):
        state = _read_id(parent_id, runs_root)
        if state is None:
            raise ChipError("no such parent branch", 404)
        return {
            "kind": "branch",
            "stack": state.get("stack") or [],
            "source_path": None,
            "state": state,
        }
    video = runs_root / run / f"{parent_id}.mp4"
    meta = runs_root / run / f"{parent_id}.json"
    if video.is_file():
        return {"kind": "variant", "stack": [], "source_path": str(video.resolve()), "variant": parent_id}
    if meta.is_file():
        raise ChipError(f"variant {parent_id} has no video file on disk", 400)
    raise ChipError("unknown parent_variant_id", 404)


def _find_source(run, runs_root, root):
    man_path = runs_root / run / "manifest.json"
    if man_path.is_file():
        try:
            man = json.loads(man_path.read_text())
        except json.JSONDecodeError:
            man = {}
        src = man.get("source") if isinstance(man, dict) else None
        if isinstance(src, str) and src.strip():
            raw = Path(src)
            candidates = []
            if raw.is_absolute():
                candidates.append(raw)
            else:
                candidates.append(root / raw)
            candidates.append(root / "samples" / raw.name)
            candidates.append(runs_root / run / raw.name)
            for candidate in candidates:
                if candidate.is_file():
                    return candidate.resolve()
    staged = sorted((runs_root / run).glob("src_*.mp4"))
    if staged:
        return staged[0].resolve()
    raise ChipError("no source video on disk for this run. Put the mp4 next to manifest.json or under samples/.", 400)


def _context_manifest(parent, run, runs_root):
    if parent["kind"] == "variant":
        path = runs_root / run / f"{parent['variant']}.json"
        data = _read_json(path)
        if isinstance(data, dict):
            return data
    if parent["kind"] == "branch":
        state = parent.get("state") or {}
        return {
            "variant_id": state.get("branch_id") or "branch",
            "hook_text": _hook_text(state.get("stack") or []),
            "duration_s": _guess_duration(state.get("stack") or []),
            "aspect": _aspect_of(state.get("stack") or []),
        }
    data = _read_json(runs_root / run / "v01.json")
    if isinstance(data, dict):
        return data
    return {"variant_id": "source", "duration_s": 30, "aspect": "9:16"}


def _guess_duration(stack):
    dur = 30.0
    for chip in stack or []:
        if chip.get("type") == "length":
            dur = float((chip.get("params") or {}).get("seconds") or dur)
    return dur


def _hook_text(stack):
    text = ""
    for chip in stack or []:
        if chip.get("text"):
            text = chip["text"]
    return text


def _aspect_of(stack):
    aspect = "9:16"
    for chip in stack or []:
        if chip.get("type") == "crop":
            aspect = (chip.get("params") or {}).get("aspect") or aspect
    return aspect


def _music_placeholder(stack):
    for chip in stack or []:
        if chip.get("type") == "music" and (chip.get("params") or {}).get("track") not in (None, "none"):
            return True
    return False


def _overlay_text(state):
    chip = state["chip"]
    for entry in reversed(state.get("stack") or []):
        if entry.get("type") == chip["type"] and entry.get("text"):
            return entry["text"]
    return "This is the cut"


def _input_media(state, stage, runs_root):
    if state.get("parent_kind") != "branch":
        path = Path(state["source_path"])
        if not path.is_file():
            raise RuntimeError("source video disappeared")
        return path
    parent = _read_id(state["parent_variant_id"], runs_root)
    if parent is None:
        raise RuntimeError("parent branch disappeared")
    directory = _branch_dir(parent, runs_root)
    video = directory / "video.mp4"
    preview = directory / "preview.mp4"
    if stage == "full":
        if not video.is_file():
            raise RuntimeError("parent full render is missing")
        return video
    if parent.get("status") == "done" and video.is_file():
        return video
    if preview.is_file():
        return preview
    if video.is_file():
        return video
    raise RuntimeError("parent preview is missing")


def _branch_dir(state, runs_root):
    return Path(runs_root) / state["run"] / "branches" / state["branch_id"]


def _new_id(runs_root):
    for _ in range(6):
        bid = "c_" + uuid.uuid4().hex[:8]
        if not (Path(runs_root) / "_chip_index" / f"{bid}.json").exists():
            return bid
    raise ChipError("could not allocate a branch id", 500)


def _write_index(bid, run, runs_root):
    directory = Path(runs_root) / "_chip_index"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{bid}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"branch_id": bid, "run": run}))
    os.replace(tmp, path)


def _write_state(state, runs_root):
    clean = {k: v for k, v in state.items() if k != "_src"}
    path = _branch_dir(clean, runs_root) / "branch.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(clean, indent=1))
    os.replace(tmp, path)


def _read_id(branch_id, runs_root):
    index = Path(runs_root) / "_chip_index" / f"{branch_id}.json"
    meta = _read_json(index)
    if not isinstance(meta, dict) or not meta.get("run"):
        return None
    return _read_json(Path(runs_root) / meta["run"] / "branches" / branch_id / "branch.json")


def _read_json(path):
    path = Path(path)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _read_job(job):
    return _read_id(job["id"], Path(job["runs_root"]))


def _fail_job(job, exc):
    runs_root = Path(job["runs_root"])
    state = _read_id(job["id"], runs_root)
    if state is None or state.get("status") in ("done", "error"):
        return
    message = str(exc).strip() or exc.__class__.__name__
    state["status"] = "error"
    state["error"] = message[:500]
    _write_state(state, runs_root)
    _poke()
    print(f"[chip] {job['id']} error {state['error']}", flush=True)


def _ensure_scheduler():
    global _STARTED, _POOL
    with _CV:
        if _POOL is None:
            _POOL = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="chip")
        if not _STARTED:
            _STARTED = True
            threading.Thread(target=_sched_loop, name="chip-sched", daemon=True).start()


def _enqueue(template, stage):
    job = {"id": template["id"], "runs_root": template["runs_root"], "root": template["root"], "stage": stage}
    with _CV:
        _PENDING.append(job)
        _CV.notify()


def _poke():
    with _CV:
        _CV.notify_all()


def _sched_loop():
    global _INFLIGHT
    while True:
        job = None
        with _CV:
            while job is None:
                if _INFLIGHT < WORKERS:
                    job = _take_ready()
                if job is not None:
                    _INFLIGHT += 1
                    break
                if not _PENDING and _INFLIGHT == 0:
                    _CV.wait()
                else:
                    _CV.wait(timeout=0.3)
        _POOL.submit(_worker, job)


def _worker(job):
    global _INFLIGHT
    try:
        run_stage(job)
        if job["stage"] == "preview":
            state = _read_job(job)
            if state and state.get("status") == "preview":
                _enqueue(job, "full")
    finally:
        with _CV:
            _INFLIGHT -= 1
            _CV.notify_all()


def _take_ready():
    """Caller holds _CV. Claim one job whose parent media is ready."""
    if not _PENDING:
        return None
    waiting = []
    chosen = None
    rest = []
    for job in _PENDING:
        if chosen is not None:
            rest.append(job)
            continue
        verdict = _inspect(job)
        if verdict == "ready":
            chosen = job
        elif verdict == "wait":
            waiting.append(job)
    _PENDING[:] = waiting + rest
    if chosen is not None:
        state = _read_job(chosen)
        if state is not None:
            state["claim"] = chosen["stage"]
            _write_state(state, Path(chosen["runs_root"]))
    return chosen


def _inspect(job):
    state = _read_job(job)
    if state is None:
        return "drop"
    if state.get("status") in ("done", "error"):
        return "drop"
    stage = job["stage"]
    if state.get("claim") == stage:
        return "drop"
    if stage == "preview" and state.get("status") != "queued":
        return "drop"
    if stage == "full" and state.get("status") != "preview":
        return "wait" if state.get("status") == "queued" else "drop"
    parent = _parent_verdict(state, stage, Path(job["runs_root"]))
    if parent == "ready":
        return "ready"
    if parent == "wait":
        return "wait"
    _fail_open(state, Path(job["runs_root"]), parent)
    return "drop"


def _parent_verdict(state, stage, runs_root):
    if state.get("parent_kind") != "branch":
        path = Path(state.get("source_path") or "")
        if path.is_file():
            return "ready"
        return "source video disappeared"
    parent = _read_id(state["parent_variant_id"], runs_root)
    if parent is None:
        return "parent branch disappeared"
    if parent.get("status") == "error":
        return "parent branch failed"
    directory = _branch_dir(parent, runs_root)
    video = directory / "video.mp4"
    preview = directory / "preview.mp4"
    if stage == "full":
        if parent.get("status") == "done" and video.is_file():
            return "ready"
        return "wait"
    if parent.get("status") in ("preview", "done") and (preview.is_file() or video.is_file()):
        return "ready"
    return "wait"


def _fail_open(state, runs_root, message):
    if state.get("status") in ("done", "error"):
        return
    state["status"] = "error"
    state["error"] = message
    _write_state(state, runs_root)
