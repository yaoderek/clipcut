"""Clip cutter: one raw video -> N short-form variants + JSON manifest."""
import json, os, re, subprocess, itertools, random
from pathlib import Path

def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"cmd failed: {' '.join(cmd)}\n{r.stderr[-2000:]}")
    return r

def probe_duration(path):
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)])
    return float(r.stdout.strip())

def has_audio(path):
    r = run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", str(path)])
    return bool(r.stdout.strip())

def transcribe(path, model_size="tiny"):
    """faster-whisper if available; fallback = evenly spaced placeholder segments."""
    try:
        from faster_whisper import WhisperModel
        m = WhisperModel(model_size, device="cpu", compute_type="float32")
        import numpy as np  # decode with ffmpeg ourselves (avoids PyAV version issues)
        pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "s16le", "-ac", "1", "-ar", "16000", "-"],
                             capture_output=True, check=True).stdout
        audio = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0
        segs, _ = m.transcribe(audio, vad_filter=True)
        out = [{"start": s.start, "end": s.end, "text": s.text.strip()} for s in segs]
        if out:
            return out, "faster-whisper:" + model_size
    except Exception as e:
        print("[transcribe] faster-whisper unavailable/failed:", e)
    d = probe_duration(path)
    return [{"start": t, "end": min(t + 3, d), "text": f"segment {i+1}"}
            for i, t in enumerate(range(0, int(d), 3))], "fallback:uniform"

def detect_silences(path, noise="-30dB", min_d=0.4):
    if not has_audio(path):
        return []
    r = subprocess.run(["ffmpeg", "-i", str(path), "-af", f"silencedetect=noise={noise}:d={min_d}", "-f", "null", "-"],
                       capture_output=True, text=True)
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", r.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", r.stderr)]
    return list(zip(starts, ends + [probe_duration(path)] * (len(starts) - len(ends))))

def speech_spans(dur, silences, start, end):
    """Keep-intervals within [start,end] after removing silences (jump cuts)."""
    spans, t = [], start
    for s, e in silences:
        if e <= start or s >= end: continue
        if s > t: spans.append((t, min(s, end)))
        t = max(t, e)
    if t < end: spans.append((t, end))
    return [(a, b) for a, b in spans if b - a > 0.25]

def hook_score(seg):
    """Cheap heuristic for hook strength: questions, numbers, 'you', exclamations, short punchy lines."""
    t = seg["text"].lower()
    s = 0.0
    s += 1.0 * ("?" in t) + 0.8 * bool(re.search(r"\d", t)) + 0.6 * bool(re.search(r"\byou(r)?\b", t))
    s += 0.5 * ("!" in t) + 0.7 * bool(re.search(r"\b(secret|never|stop|why|how|best|worst|mistake)\b", t))
    s += 0.3 * (len(t.split()) <= 12)
    return s

CAPTION_STYLES = {
    "bold_yellow": "FontName=DejaVu Sans,Fontsize=18,Bold=1,PrimaryColour=&H0000FFFF,OutlineColour=&H00000000,Outline=3,Alignment=2,MarginV=60",
    "white_box":   "FontName=DejaVu Sans,Fontsize=14,PrimaryColour=&H00FFFFFF,BackColour=&H80000000,BorderStyle=4,Alignment=2,MarginV=40",
    "minimal":     "FontName=DejaVu Sans,Fontsize=11,PrimaryColour=&H00FFFFFF,Outline=1,Alignment=2,MarginV=25",
    "none": None,
}

def fmt_ts(t):
    h, m = int(t // 3600), int(t % 3600 // 60)
    return f"{h:02d}:{m:02d}:{t%60:06.3f}".replace(".", ",")

def write_srt(segments, spans, path):
    """Map source-time transcript segments onto the output timeline defined by spans."""
    lines, n, off = [], 1, 0.0
    for a, b in spans:
        for s in segments:
            s0, s1 = max(s["start"], a), min(s["end"], b)
            if s1 - s0 > 0.2:
                lines += [str(n), f"{fmt_ts(off + s0 - a)} --> {fmt_ts(off + s1 - a)}", s["text"], ""]
                n += 1
        off += b - a
    Path(path).write_text("\n".join(lines))

def plan_variants(dur, segments, silences, n=8, seed=0):
    rng = random.Random(seed)
    hooks = sorted(segments, key=hook_score, reverse=True)[:4] or [{"start": 0, "end": 3, "text": ""}]
    lengths = [L for L in (15, 30, 45) if L <= dur + 1] or [int(dur)]
    combos = list(itertools.product(range(len(hooks)), lengths, [True, False], list(CAPTION_STYLES), [True, False]))
    rng.shuffle(combos)
    # round-robin target lengths so every length bucket is represented
    combos.sort(key=lambda c: 0)  # keep shuffled order
    by_len = {L: [c for c in combos if c[1] == L] for L in lengths}
    combos = [c for tup in itertools.zip_longest(*by_len.values()) for c in tup if c]
    # make sure the diversity is real: greedily pick combos differing from previous picks
    picked = []
    for c in combos:
        if all(sum(x != y for x, y in zip(c, p)) >= 2 for p in picked):
            picked.append(c)
        if len(picked) >= n: break
    plans = []
    for i, (hi, L, jump, cap, zoom) in enumerate(picked):
        h = hooks[hi]
        hs = max(0.0, h["start"]); he = min(dur, hs + 3.0)
        # body: content after the hook start, wrap to beginning if needed; skip hook window
        body_src = [(he, dur), (0.0, hs)]
        spans = [(hs, he)]
        need = L - 3.0
        for a, b in body_src:
            if need <= 0: break
            pieces = speech_spans(dur, silences, a, b) if jump else [(a, b)]
            for pa, pb in pieces:
                if need <= 0: break
                take = min(pb - pa, need); spans.append((pa, pa + take)); need -= take
        out_len = sum(b - a for a, b in spans)
        n_cuts = len(spans) - 1
        plans.append(dict(variant_id=f"v{i+1:02d}", hook_start=round(hs, 2), hook_text=h["text"],
                          hook_score=round(hook_score(h), 2), target_length_s=L, length_s=round(out_len, 2),
                          jump_cuts=jump, n_cuts=n_cuts, cuts_per_min=round(60 * n_cuts / max(out_len, 1), 2),
                          caption_style=cap, hook_zoom=zoom, spans=[(round(a, 3), round(b, 3)) for a, b in spans]))
    return plans

def render(src, plan, segments, outdir, W=720, H=1280):
    outdir = Path(outdir); vid = plan["variant_id"]
    audio = has_audio(src)
    parts = []
    for k, (a, b) in enumerate(plan["spans"]):
        p = outdir / f".{vid}_p{k}.mp4"
        vf = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1"
        if k == 0 and plan["hook_zoom"]:
            vf += f",zoompan=z='min(1+0.006*on,1.25)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={W}x{H}:fps=30"
        cmd = ["ffmpeg", "-y", "-v", "error", "-ss", str(a), "-t", str(b - a), "-i", str(src), "-vf", vf, "-r", "30",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "26"]
        cmd += (["-c:a", "aac", "-ar", "44100", "-ac", "2"] if audio else ["-an"])
        run(cmd + [str(p)]); parts.append(p)
    lst = outdir / f".{vid}_list.txt"
    lst.write_text("".join(f"file '{p.resolve()}'\n" for p in parts))
    joined = outdir / f".{vid}_joined.mp4"
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(joined)])
    final = outdir / f"{vid}.mp4"
    style = CAPTION_STYLES[plan["caption_style"]]
    if style:
        srt = outdir / f"{vid}.srt"; write_srt(segments, plan["spans"], srt)
        run(["ffmpeg", "-y", "-v", "error", "-i", str(joined), "-vf", f"subtitles={srt}:force_style='{style}'",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-c:a", "copy", str(final)])
    else:
        os.replace(joined, final)
    for p in parts + [lst, joined]:
        if p.exists(): p.unlink()
    plan["file"] = str(final)
    return plan

def cut(src, outdir, n=8, seed=0, model_size="tiny"):
    outdir = Path(outdir); outdir.mkdir(parents=True, exist_ok=True)
    dur = probe_duration(src)
    segments, tx = transcribe(src, model_size)
    silences = detect_silences(src)
    plans = plan_variants(dur, segments, silences, n=n, seed=seed)
    for p in plans:
        print(f"[render] {p['variant_id']} len={p['length_s']}s jump={p['jump_cuts']} cap={p['caption_style']} zoom={p['hook_zoom']}")
        render(src, p, segments, outdir)
    manifest = dict(source=str(src), source_duration_s=round(dur, 2), transcriber=tx,
                    n_transcript_segments=len(segments), n_silences=len(silences), variants=plans)
    (outdir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest
