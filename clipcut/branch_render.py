"""Render one cutter_v2 recipe into a run directory.

Called as `python -m clipcut.branch_render job.json`. Uses clipcut.cutter_v2.render
(ffmpeg, face crop, ASS captions). Not a mockup. Transcription prefers an SRT or
an existing transcript.json so a branch does not have to re-run Whisper.
"""
import json, sys
from pathlib import Path

def _status(outdir, phase, **extra):
    payload = dict(phase=phase, **extra)
    (Path(outdir) / "status.json").write_text(json.dumps(payload))
    print(f"[branch] {phase}", flush=True)

def _load_transcript(src, outdir, srt, srt_offset):
    from clipcut import cutter_v2 as C
    cache = Path(outdir) / "transcript.json"
    if srt:
        words, sents = C.transcript_from_srt(srt, float(srt_offset or 0))
        cache.write_text(json.dumps([words, sents]))
        return words, sents, "human-srt"
    if cache.exists():
        words, sents = json.loads(cache.read_text())
        return words, sents, "cached-transcript"
    _status(outdir, "transcribing", note="faster-whisper base, CPU. This is the slow step.")
    words, sents = C.transcribe_words(src, "base")
    cache.write_text(json.dumps([words, sents]))
    return words, sents, "faster-whisper:base"

def render_job(job):
    from clipcut.cutter import detect_silences, probe_duration
    from clipcut import cutter_v2 as C
    from clipcut.edit_idea import _coerce

    src = Path(job["src"])
    outdir = Path(job["outdir"])
    outdir.mkdir(parents=True, exist_ok=True)
    if not src.is_file():
        raise FileNotFoundError(f"source video missing: {src}")
    settings = _coerce(job.get("settings") or {})
    _status(outdir, "silences")
    C.SILENCES = detect_silences(src, noise="-35dB", min_d=0.25)
    _status(outdir, "transcript")
    words, sents, how = _load_transcript(src, outdir, job.get("srt"), job.get("srt_offset"))
    dur = probe_duration(src)
    sents = [x for x in sents if x.get("end", 0) <= dur - 0.05 and len(x.get("words") or []) >= 2]
    if not sents:
        raise RuntimeError("No transcript sentences to cut. Add an SRT or a video with speech.")
    _status(outdir, "face")
    fx, hit = C.face_center_x(src)
    hidx, _src_name = C.pick_hooks(sents, k=5)
    hook = settings["hook"]
    if hook == "earliest":
        hook_idx = 0
    elif hook == "second" and len(hidx) > 1:
        hook_idx = hidx[1]
    elif hook.startswith("m") and hook[1:].isdigit():
        i = int(hook[1:]) - 1
        hook_idx = hidx[i] if 0 <= i < len(hidx) else (hidx[0] if hidx else 0)
    else:
        hook_idx = hidx[0] if hidx else 0
    names = [f"m{i+1}" for i in range(len(hidx))]
    hook_name = "m1"
    for name, idx in zip(names, hidx):
        if idx == hook_idx:
            hook_name = name
            break
    if hook == "earliest":
        hook_name = "earliest"
    aspect = settings["aspect"]
    plan = dict(
        hook=hook_name, length=settings["length"], pacing=settings["pacing"], captions=settings["captions"],
        hook_title=settings["hook_title"], ending=settings["ending"], hook_idx=hook_idx,
        channel=settings["channel"], aspect=aspect,
        resolution=C.ASPECT_RES.get(aspect, (1080, 1920)))
    vid = "v01"
    _status(outdir, "rendering", settings=settings, transcriber=how)
    meta = C.render(src, sents, plan, vid, outdir, fx)
    manifest = json.loads((outdir / f"{vid}.json").read_text())
    (outdir / "manifest.json").write_text(json.dumps(dict(
        source=str(src), source_duration_s=dur, transcriber=how, n_words=len(words),
        face_x=fx, face_hit=hit, settings=settings, variants=[meta]), indent=1))
    _status(outdir, "done", variant_id=vid, video=f"{vid}.mp4")
    return manifest

def main():
    job_path = Path(sys.argv[1])
    job = json.loads(job_path.read_text())
    try:
        render_job(job)
    except Exception as e:
        outdir = Path(job.get("outdir") or ".")
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / "status.json").write_text(json.dumps(dict(phase="error", error=str(e))))
        print(f"[branch] error {e}", flush=True)
        raise

if __name__ == "__main__":
    main()
