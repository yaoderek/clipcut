"""Clip cutter v2 — short-form recipe aware.
Recipe dimensions varied across variants (all recorded in manifest):
  hook        : which sentence opens (best-scored / 2nd / chronological). Video starts ON the first word (no dead air).
  length      : 15 / 30 / 45 s target, ends on a sentence boundary.
  pacing      : 'raw' (sentence gaps kept), 'tight' (every word gap > 0.25 s removed = jump cuts),
                'tight_punch' (tight + punch-in zoom alternating every ~3 s = pattern interrupts).
  captions    : 'none' | 'sentence' (lower third) | 'word_pop' (1-3 words, big, bold, lower-middle).
  hook_title  : text overlay of the hook line at top for first 3 s.
  ending      : 'loop' (hard cut on last word) | 'fade' (0.6 s fade to black).
Always: 9:16 1080x1920 face-aware crop."""
import json, re, subprocess, itertools, random, numpy as np, cv2
from pathlib import Path
from .cutter import run, probe_duration, hook_score
W, H = 1080, 1920

def transcribe_words(path, model_size="base"):
    from faster_whisper import WhisperModel
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "s16le", "-ac", "1", "-ar", "16000", "-"], capture_output=True, check=True).stdout
    audio = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0
    m = WhisperModel(model_size, device="cpu", compute_type="float32")  # int8 returns garbage with ctranslate2 4.8 on this CPU
    segs, _ = m.transcribe(audio, vad_filter=True, word_timestamps=True, language="en", condition_on_previous_text=False)
    words = [dict(w=w.word.strip(), s=w.start, e=w.end) for s in segs for w in (s.words or [])]
    sents, cur = [], []
    for w in words:
        cur.append(w)
        if re.search(r"[.?!]$", w["w"]) or len(cur) > 25: sents.append(cur); cur = []
    if cur: sents.append(cur)
    return words, [dict(start=s[0]["s"], end=s[-1]["e"], text=" ".join(x["w"] for x in s), words=s) for s in sents]

def transcript_from_srt(srt, offset=0.0):
    """Fallback when Whisper is too slow/unreliable: human SRT, word times interpolated uniformly within each cue."""
    words = []
    for blk in Path(srt).read_text(encoding="utf-8").strip().split("\n\n"):
        ln = blk.strip().splitlines()
        if len(ln) < 3: continue
        tt = lambda x: sum(float(v) * m for v, m in zip(x.replace(",", ".").split(":"), (3600, 60, 1)))
        a, b = [tt(x.strip()) - offset for x in ln[1].split("-->")]
        ws = " ".join(ln[2:]).replace("’", "'").split()
        if b <= 0 or not ws: continue
        for i, w in enumerate(ws):
            words.append(dict(w=w, s=a + (b - a) * i / len(ws), e=a + (b - a) * (i + 1) / len(ws)))
    words = [w for w in words if w["s"] >= 0]
    sents, cur = [], []
    for w in words:
        cur.append(w)
        if re.search(r"[.?!]$", w["w"]) or len(cur) > 25: sents.append(cur); cur = []
    if cur: sents.append(cur)
    return words, [dict(start=x[0]["s"], end=x[-1]["e"], text=" ".join(y["w"] for y in x), words=x) for x in sents]

def face_center_x(path, n=12):
    det = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    cap = cv2.VideoCapture(str(path)); N = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)); xs = []
    for i in np.linspace(0, N - 1, n).astype(int):
        cap.set(cv2.CAP_PROP_POS_FRAMES, i); ok, f = cap.read()
        if not ok: continue
        g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY); fs = det.detectMultiScale(g, 1.2, 5, minSize=(40, 40))
        if len(fs): x, y, w, h = max(fs, key=lambda r: r[2] * r[3]); xs.append((x + w / 2) / g.shape[1])
    return (float(np.median(xs)) if xs else 0.5), len(xs) / n

HOOK_MIN, HOOK_MAX = 1.2, 2.9
LAST_HOOK = {}

def hook_phrase(sent, start=None, max_s=HOOK_MAX, min_s=HOOK_MIN):
    """Cold-open phrase of the hook sentence, measured from the real first-frame time `start`.
    Candidate cut points = word ends within [min_s, max_s] that sit on punctuation or on a detected pause (ffmpeg silencedetect
    silence starting within 0.2 s after the word). Prefer punctuation+pause > pause > punctuation > latest word end.
    Returns (cut_time_src, text, boundary_kind). Cut time is snapped into the pause when there is one."""
    ws = sent["words"]; a = sent["start"] if start is None else start
    best = None
    for k, w in enumerate(ws):
        d = w["e"] - a
        if d < min_s or d > max_s: continue
        punct = bool(re.search(r"[,.?!;:]$", w["w"]))
        pause = next(((q0, q1) for q0, q1 in SILENCES if -0.1 <= q0 - w["e"] <= 0.2), None)
        rank = (2 if pause else 0) + (1 if punct else 0)
        cut = min(pause[0] + 0.1, pause[1]) if pause else w["e"] + 0.02
        cut = min(cut, a + max_s + 0.08)
        kind = "+".join(x for x, f in (("pause", pause), ("punct", punct)) if f) or "word"
        if best is None or (rank, k) >= (best[0], best[1]): best = (rank, k, cut, kind)
    if best is None:  # no word ends in window: cut at the window edge
        k = max([i for i, w in enumerate(ws) if w["e"] - a <= max_s] or [0]); cut = max(a + min_s, min(ws[k]["e"], a + max_s))
        return cut, " ".join(w["w"] for w in ws[:k + 1]), "forced"
    return best[2], " ".join(w["w"] for w in ws[:best[1] + 1]), best[3]

def build_spans(sents, hook_idx, target, pacing):
    order = [hook_idx] + list(range(hook_idx + 1, len(sents))) + list(range(0, hook_idx))
    spans, total = [], 0.0
    for i in order:
        s = sents[i]
        if pacing.startswith("raw"):
            pieces = [(s["start"], s["end"] + 0.35)]
        else:  # jump cuts: remove every detected silence > 0.25 s inside the sentence (ffmpeg silencedetect)
            pieces, a = [], s["start"]
            for q0, q1 in SILENCES:
                if q1 <= a or q0 >= s["end"]: continue
                if q0 - a > 0.15: pieces.append((a, q0 + 0.05))
                a = max(a, q1 - 0.05)
            if s["end"] - a > 0.15: pieces.append((a, s["end"] + 0.08))
            if not pieces: pieces = [(s["start"], s["end"])]
        if i == hook_idx:  # hard cut after the hook phrase (snapped to pause/punctuation), hook 1.2-3 s
            pe, htxt, kind = hook_phrase(s, start=pieces[0][0]); sp = []
            LAST_HOOK.clear(); LAST_HOOK.update(cut=pe, text=htxt, kind=kind)
            for a, b in pieces:
                if a + 0.15 < pe < b - 0.15: sp += [(a, pe), (pe, b)]
                else: sp.append((a, b))
            pieces = sp
        dur = sum(b - a for a, b in pieces)
        if total + dur > target + 2.5 and total >= target * 0.75: break
        spans += pieces; total += dur
    out = []
    for a, b in spans:
        if pacing.endswith("_punch"):
            while b - a > 3.2: out.append((a, a + 3.0)); a += 3.0
        out.append((a, b))
    return [(round(a, 3), round(b, 3)) for a, b in out if b - a > 0.15]

def _t(t): return f"{int(t//3600)}:{int(t%3600//60):02d}:{t%60:05.2f}"

def write_ass(path, sents, spans, style, hook_title, hook_text):
    f = H / 1920; fs = max(0.72, f); M = lambda v: int(v * f); F = lambda v: int(v * fs)
    hdr = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: sentence,DejaVu Sans,{F(58)},&H00FFFFFF,&H00FFFFFF,&H00000000,&H90000000,1,0,0,0,100,100,0,0,4,2,0,2,90,90,{M(560)},1
Style: pop,DejaVu Sans,{F(96)},&H0000F0FF,&H00FFFFFF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,7,3,2,60,60,{M(620)},1
Style: title,DejaVu Sans,{F(64)},&H00FFFFFF,&H00FFFFFF,&H00000000,&HC0000000,1,0,0,0,100,100,0,0,3,14,0,8,70,70,{M(220)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    def remap(t):
        off = 0.0
        for a, b in spans:
            if a - 1e-3 <= t <= b + 1e-3: return off + (min(t, b) - a)
            off += b - a
        return None
    ev = []
    if style == "sentence":
        for s in sents:
            ok = [(remap(w["s"]), remap(w["e"])) for w in s["words"]]; ok = [x for x in ok if None not in x]
            if ok: ev.append(("sentence", ok[0][0], ok[-1][1], s["text"]))
    elif style == "word_pop":
        chunk = []
        for w in (w for s in sents for w in s["words"]):
            a, b = remap(w["s"]), remap(w["e"])
            if a is None or b is None: continue
            chunk.append((a, b, w["w"]))
            if len(chunk) == 3 or re.search(r"[.?!,]$", w["w"]):
                ev.append(("pop", chunk[0][0], chunk[-1][1] + 0.05, " ".join(c[2] for c in chunk).upper())); chunk = []
        if chunk: ev.append(("pop", chunk[0][0], chunk[-1][1], " ".join(c[2] for c in chunk).upper()))
    if hook_title:
        ev.append(("title", 0.0, 3.0, hook_text if len(hook_text) < 60 else hook_text[:57] + "..."))
    Path(path).write_text(hdr + "".join(f"Dialogue: 0,{_t(a)},{_t(b)},{st},,0,0,0,,{tx}\n" for st, a, b, tx in ev))

# Fallback channel specs; clipcut/channels.py (retention agent) is preferred when it imports cleanly.
FALLBACK_CHANNELS = {15: ["TikTok", "Shorts"], 30: ["Reels", "TikTok"], 45: ["Shorts", "X", "LinkedIn"]}

def _channels_mod():
    try:
        from . import channels as C; C.CHANNEL_SPECS; return C
    except Exception: return None

def pick_hooks(sents, k=5):
    """Ranked, distinct hook sentence indices. Prefer channels.pick_moments; fall back to heuristic hook_score."""
    C = _channels_mod()
    if C is not None and hasattr(C, "pick_moments"):
        try:
            spans = C.pick_moments([dict(start=x["start"], end=x["end"], text=x["text"]) for x in sents], n=k, min_len=15, max_len=45)
            idx = []
            for sp in spans:
                st = sp.get("src_start", sp.get("start")) if isinstance(sp, dict) else sp[0]
                i = min(range(len(sents)), key=lambda j: abs(sents[j]["start"] - st))
                if i not in idx: idx.append(i)
            if idx: return idx, "channels.pick_moments"
        except Exception as e: print("[v2] channels.pick_moments failed, using fallback:", e)
    ranked = sorted(range(len(sents)), key=lambda i: -(hook_score(sents[i]) + 0.3 * (3 <= sents[i]["end"] - sents[i]["start"] <= 8)))
    idx = ranked[:k - 1] + ([0] if 0 not in ranked[:k - 1] else ranked[k - 1:k])
    return idx, "heuristic hook_score"

def channel_for(length, i):
    C = _channels_mod()
    if C is not None:
        try:
            V = {n: sp for n, sp in C.CHANNEL_SPECS.items() if "9:16" in sp.get("aspect", ["9:16"])}  # we render 9:16 only
            fits = [n for n, sp in V.items() if sp["ideal_s"][0] <= length <= sp["ideal_s"][1]] or list(V)
            ideal = sorted(fits, key=lambda n: abs(sum(V[n]["ideal_s"]) / 2 - length))
            if ideal: return ideal[i % len(ideal)]
        except Exception: pass
    opts = FALLBACK_CHANNELS[length]; return opts[i % len(opts)]

def plan(sents, dur, n=10, seed=0):
    """Every variant: captions + punch-ins + loop ending (house rules). Varied: hook moment, length, pacing, caption style, hook title."""
    rng = random.Random(seed)
    hidx, src = pick_hooks(sents); names = [f"m{i+1}" for i in range(len(hidx))]; hooks = dict(zip(names, hidx))
    print(f"[v2] hook moments via {src}: {hidx}")
    lengths = [L for L in (15, 30, 45) if L <= dur]
    dims = dict(hook=names, length=lengths, pacing=["raw_punch", "tight_punch"], captions=["sentence", "word_pop"],
                hook_title=[False, True], ending=["loop"])
    allc = list(itertools.product(*dims.values())); rng.shuffle(allc)
    picked = [("m1", lengths[0], "tight_punch", "word_pop", True, "loop")]
    for thr in (4, 3, 2, 1):
        for c in allc:
            if len(picked) >= n: break
            if c not in picked and min(sum(x != y for x, y in zip(c, p)) for p in picked) >= thr: picked.append(c)
    out = []
    for i, c in enumerate(picked):
        d = dict(zip(dims, c)) | dict(hook_idx=hooks[c[0]]); d["channel"] = channel_for(d["length"], i); out.append(d)
    return out + channel_variants(hooks, lengths)

# Non-9:16 channel formats for the top moments (CHANNEL_SPECS from clipcut/channels.py when available)
FALLBACK_FORMATS = {"X": ("1:1", 30), "LinkedIn": ("4:5", 45)}
ASPECT_RES = {"1:1": (1080, 1080), "4:5": (1080, 1350), "16:9": (1280, 720), "9:16": (1080, 1920)}

def channel_variants(hooks, lengths, top=2):
    C = _channels_mod(); out = []
    for ch, (asp, L) in FALLBACK_FORMATS.items():
        if C is not None and ch in getattr(C, "CHANNEL_SPECS", {}):
            sp = C.CHANNEL_SPECS[ch]; al = sp.get("aspect", [asp])
            asp = "4:5" if "4:5" in al else ("1:1" if "1:1" in al else al[0])  # talking head: prefer the tallest allowed frame
            lo, hi = sp.get("ideal_s", (L, L)); L = min([x for x in lengths if lo <= x <= hi] or [L], key=lambda x: abs(x - (lo + hi) / 2))
        for m in list(hooks)[:top]:
            out.append(dict(hook=m, length=L, pacing="tight_punch", captions="sentence", hook_title=True, ending="loop",
                            hook_idx=hooks[m], channel=ch, aspect=asp, resolution=ASPECT_RES[asp]))
    return out

def render(src, sents, p, vid, outdir, fx):
    global W, H
    W, H = p.get("resolution", (1080, 1920))
    outdir = Path(outdir); spans = build_spans(sents, p["hook_idx"], p["length"], p["pacing"]); p["_hook"] = dict(LAST_HOOK)
    pr = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0", str(src)], capture_output=True, text=True)
    sw, sh = [int(x) for x in pr.stdout.strip().split(",")[:2]]
    parts = []
    for k, (a, b) in enumerate(spans):
        z = 1.0
        if p["pacing"].endswith("_punch"): z = 1.08 if k == 0 else (1.2 if k % 2 else 1.0)
        ch = sh / z; cw = min(ch * W / H, sw / z); ch = cw * H / W
        x = min(max(fx * sw - cw / 2, 0), sw - cw); y = (sh - ch) * 0.35
        vf = f"crop={int(cw)//2*2}:{int(ch)//2*2}:{int(x)}:{int(y)},scale={W}:{H},setsar=1,fps=30"
        q = outdir / f".{vid}_{k:03d}.mp4"
        run(["ffmpeg", "-y", "-v", "error", "-ss", str(a), "-t", str(b - a), "-i", str(src), "-vf", vf, "-c:v", "libx264", "-preset", "veryfast",
             "-crf", "23", "-c:a", "aac", "-ar", "44100", "-ac", "2", str(q)])
        parts.append(q)
    lst = outdir / f".{vid}.txt"; lst.write_text("".join(f"file '{q.resolve()}'\n" for q in parts))
    joined = outdir / f".{vid}_j.mp4"
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(joined)])
    L = probe_duration(joined); ass = outdir / f"{vid}.ass"
    write_ass(ass, sents, spans, p["captions"], p["hook_title"], sents[p["hook_idx"]]["text"])
    fade = p["ending"] == "fade"
    vf = f"ass={ass}" + (f",fade=t=out:st={max(L-0.6,0):.2f}:d=0.6" if fade else "")
    af = f"afade=t=out:st={max(L-0.6,0):.2f}:d=0.6" if fade else "anull"
    out = outdir / f"{vid}.mp4"
    run(["ffmpeg", "-y", "-v", "error", "-i", str(joined), "-vf", vf, "-af", af, "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-c:a", "aac", str(out)])
    for q in parts + [lst, joined]: q.unlink()
    n_cuts = len(spans) - 1
    write_variant_manifest(outdir, vid, p, spans, sents, L, out, ass)
    return dict(variant_id=vid, **p, hook_text=sents[p["hook_idx"]]["text"], length_s=round(L, 2),
                n_cuts=n_cuts, cuts_per_10s=round(10 * n_cuts / L, 2), spans=spans, file=str(out))

def write_variant_manifest(outdir, vid, p, spans, sents, L, out, ass):
    """Per-variant manifest (shared schema with clipcut/scorer.py)."""
    segs, o = [], 0.0
    for k, (a, b) in enumerate(spans):
        z = (1.08 if k == 0 else (1.2 if k % 2 else 1.0)) if p["pacing"].endswith("_punch") else 1.0
        segs.append(dict(src_start=a, src_end=b, out_start=round(o, 3), punch_in=z)); o += b - a
    caps = []
    for ln in Path(ass).read_text().splitlines():
        if ln.startswith("Dialogue:"):
            f = ln.split(",", 9); tt = lambda x: sum(float(v) * m for v, m in zip(x.split(":"), (3600, 60, 1)))
            if f[3] != "title": caps.append(dict(start=round(tt(f[1]), 2), end=round(tt(f[2]), 2), text=f[9]))
    hs = sents[p["hook_idx"]]; pe, htxt, hkind = p["_hook"]["cut"], p["_hook"]["text"], p["_hook"]["kind"]; hook_end, o = None, 0.0
    for a, b in spans:  # output time of the hook cut; if the cut sits in a removed pause, the hook ends at the last kept frame before it
        if a - 1e-3 <= pe <= b + 1e-3: hook_end = o + (pe - a); break
        if a > pe: hook_end = o; break
        o += b - a
    hook_end = min(hook_end if hook_end is not None else spans[0][1] - spans[0][0], L)
    removed = round(sum(max(0, segs[i+1]["src_start"] - segs[i]["src_end"]) for i in range(len(segs) - 1)
                        if 0 < segs[i+1]["src_start"] - segs[i]["src_end"] < 2.0), 2)  # intra-sentence gaps cut out
    recipe = {k: p[k] for k in ("hook", "length", "pacing", "captions", "hook_title", "ending", "channel")} | {"aspect": p.get("aspect", "9:16")}
    m = dict(variant_id=vid, recipe=recipe, hook_text=hs["text"], hook_phrase=htxt, hook_boundary=hkind, aspect=p.get("aspect", "9:16"), resolution=[W, H], hook_src_start=hs["start"], channel=p["channel"], duration_s=round(L, 2), video_path=str(Path(out).resolve()), segments=segs, captions=caps,
             hook_end_s=round(hook_end, 2), cut_times_s=[s["out_start"] for s in segs[1:]], silence_removed_s=removed, loop_end=p["ending"] == "loop")
    (Path(outdir) / f"{vid}.json").write_text(json.dumps(m, indent=1)); return m

SILENCES = []
def cut(src, outdir, n=10, seed=0, model_size="base", srt=None, srt_offset=0.0):
    global SILENCES
    from .cutter import detect_silences
    SILENCES = detect_silences(src, noise="-35dB", min_d=0.25)
    outdir = Path(outdir); outdir.mkdir(parents=True, exist_ok=True)
    cache = outdir / "transcript.json"  # checkpoint: transcription is the slow step
    if srt: words, sents = transcript_from_srt(srt, srt_offset); model_size = "human-srt(interpolated words)"
    elif cache.exists(): words, sents = json.load(open(cache))
    else:
        words, sents = transcribe_words(src, model_size); cache.write_text(json.dumps([words, sents]))
    dur = probe_duration(src)
    sents = [x for x in sents if x["end"] <= dur - 0.1 and len(x["words"]) >= 2]
    fx, hit = face_center_x(src)
    print(f"[v2] {len(words)} words, {len(sents)} sentences, face x={fx:.2f} (found in {hit:.0%} of samples)")
    vs = []
    for i, p in enumerate(plan(sents, dur, n, seed)):
        v = render(src, sents, p, f"v{i+1:02d}", outdir, fx)
        print(f"[render] {v['variant_id']} {v['length_s']}s hook={p['hook']} pacing={p['pacing']} cap={p['captions']} title={p['hook_title']} end={p['ending']} ch={p['channel']} {p.get('aspect','9:16')} hook_end={json.load(open(Path(outdir)/(v['variant_id']+'.json')))['hook_end_s']} cuts/10s={v['cuts_per_10s']}", flush=True)
        vs.append(v)
    m = dict(source=str(src), source_duration_s=dur, transcriber=(model_size if srt else f"faster-whisper:{model_size}"), n_silences=len(SILENCES), n_words=len(words), face_x=fx, variants=vs)
    (outdir / "manifest.json").write_text(json.dumps(m, indent=1)); return m
