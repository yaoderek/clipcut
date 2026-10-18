"""ClipCut demo web UI.  Run: uvicorn app:app --host 0.0.0.0 --port 8000
New UI at /  (v2: cut2 -> scorer -> results.json). Legacy v1 form kept at /v1 (POST /process)."""
import json, shutil, uuid, subprocess, sys, re, time
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).parent.resolve(); RUNS = ROOT / "runs"; RUNS.mkdir(exist_ok=True); SAMPLES = ROOT / "samples"
SRT = {"talking_head_niosh.mp4": ("niosh_liz.en.srt", 28.0)}
TIE = 0.005
app = FastAPI(title="ClipCut"); app.mount("/runs", StaticFiles(directory=str(RUNS)), name="runs")
JOBS = {}  # run -> {proc, n, log}

def pick_winner(res):
    """Top overall.score; among variants within TIE of the top, prefer captioned ones."""
    top = res[0]["overall"]["score"]
    near = [r for r in res if top - r["overall"]["score"] <= TIE]
    cap = [r for r in near if r["recipe"].get("captions", "none") != "none"]
    return (cap or near)[0]["variant_id"], top

def run_state(run):
    d = RUNS / run
    if not d.is_dir(): raise HTTPException(404, "no such run")
    log = d / "job.log"; txt = log.read_text() if log.exists() else ""
    j = JOBS.get(run); n = j["n"] if j else 0
    rendered = len(re.findall(r"^\[render\]", txt, re.M)); scored = len(re.findall(r"^v\d\d \{", txt, re.M))
    running = bool(j and j["proc"].poll() is None)
    out = dict(run=run, running=running, rendered=rendered, scored=scored, n=n, tail=txt.strip().splitlines()[-3:])
    if j and not running and j["proc"].returncode: out["error"] = f"exit {j['proc'].returncode}"
    if (d / "results.json").exists() and not running:
        r = json.load(open(d / "results.json")); res = r["results"]
        for x in res:
            m = json.load(open(d / f"{x['variant_id']}.json")); x["video"] = f"/runs/{run}/{x['variant_id']}.mp4"
            poster = d / f".{x['variant_id']}_poster.jpg"
            if not poster.exists():
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "1.5", "-i", str(d / f"{x['variant_id']}.mp4"), "-frames:v", "1", "-vf", "scale=360:-2", str(poster)])
            x["poster"] = f"/runs/{run}/{poster.name}"
            x["hook_text"] = m.get("hook_text", ""); x["hook_phrase"] = m.get("hook_phrase"); x["aspect"] = m.get("aspect", "9:16"); x["hook_boundary"] = m.get("hook_boundary"); x["hook_end_s"] = m.get("hook_end_s"); x["target_channel"] = m.get("channel") or m["recipe"].get("channel")
            ch = x.get("channels"); sug = x.get("best_channel") or x.get("suggested_channel")
            if not sug and isinstance(ch, list) and ch: sug = ch[0].get("channel") if isinstance(ch[0], dict) else ch[0]
            x["target_channel"] = x.get("target_channel") or x["target_channel"]
            x["suggested"] = sug; x["captions_n"] = len(m["captions"])
            x["n_cuts"] = len(m["cut_times_s"]); x["loop_end"] = m["loop_end"]; x["silence_removed_s"] = m["silence_removed_s"]
        out["results"] = res; out["winner"], _ = pick_winner(res); out["model_winner"] = res[0]["variant_id"]
        out["too_close"] = len(res) > 1 and res[0]["overall"]["score"] - res[1]["overall"]["score"] <= 0.01
        out["deployed"] = f"/runs/{run}/winner.mp4" if (d / "winner.mp4").exists() else None
    return out

@app.get("/api/runs")
def runs():
    return sorted([p.name for p in RUNS.iterdir() if (p / "results.json").exists() or p.name in JOBS], key=lambda r: (r != "v2_niosh_multi", r != "v2_niosh_house", r != "v2_niosh12", r))

@app.get("/api/samples")
def samples(): return [p.name for p in SAMPLES.glob("*.mp4")]

@app.get("/api/run/{run}")
def get_run(run: str): return run_state(run)

MAX_UPLOAD_MB = 95  # Cloudflare quick tunnels cap request bodies at 100 MB

def busy_job():
    return next((r for r, j in JOBS.items() if j["proc"].poll() is None), None)

@app.get("/api/status")
def status():
    b = busy_job(); return dict(busy=bool(b), run=b, max_upload_mb=MAX_UPLOAD_MB)

@app.post("/api/cut")
def start_cut(video: UploadFile | None = File(None), sample: str = Form(""), n: int = Form(12)):
    b = busy_job()
    if b: raise HTTPException(409, f"Busy: run {b} is still rendering. One job at a time; try again when it finishes (poll /api/run/{b}).")
    rid = "ui_" + time.strftime("%H%M%S") + "_" + uuid.uuid4().hex[:4]; d = RUNS / rid; d.mkdir()
    cmd = [sys.executable, str(ROOT / "cli.py"), "cut2"]
    if video is not None and video.filename:
        src = d / ("src_" + Path(video.filename).name)
        with open(src, "wb") as f: shutil.copyfileobj(video.file, f)
        if src.stat().st_size > MAX_UPLOAD_MB * 2**20:
            src.unlink(); d.rmdir(); raise HTTPException(413, f"Upload too large (max {MAX_UPLOAD_MB} MB through the public tunnel).")
    elif sample and (SAMPLES / Path(sample).name).exists():
        src = SAMPLES / Path(sample).name
        if src.name in SRT: cmd += ["--srt", str(SAMPLES / SRT[src.name][0]), "--srt-offset", str(SRT[src.name][1])]
    else: raise HTTPException(400, "upload a video or pick a sample")
    cmd += [str(src), "--out", str(d), "-n", str(max(2, min(n, 20)))]
    JOBS[rid] = dict(proc=subprocess.Popen(cmd, cwd=ROOT, stdout=open(d / "job.log", "w"), stderr=subprocess.STDOUT), n=n)
    return {"run": rid}

@app.get("/api/channels/{run}")
def channel_blocks(run: str):
    from clipcut import channel_blocks as CB
    s = run_state(run)
    if "results" not in s: raise HTTPException(400, "run not finished")
    cache = RUNS / run / "channels_blocks.json"; deps = [RUNS / run / "results.json", ROOT / "clipcut" / "channel_blocks.py", ROOT / "clipcut" / "retention.py", ROOT / "clipcut" / "channels.py"]
    if cache.exists() and cache.stat().st_mtime > max(d.stat().st_mtime for d in deps if d.exists()): return json.load(open(cache))
    out = CB.build(RUNS / run, s["results"]); cache.write_text(json.dumps(out)); return out

@app.post("/api/deploy/{run}")
def deploy(run: str):
    s = run_state(run)
    if "winner" not in s: raise HTTPException(400, "run not finished")
    shutil.copyfile(RUNS / run / f"{s['winner']}.mp4", RUNS / run / "winner.mp4")
    (RUNS / run / "winner.json").write_text(json.dumps(dict(winner=s["winner"], model_top=s["model_winner"], tie_rule=f"captioned preferred within {TIE}"), indent=1))
    return {"winner": s["winner"], "url": f"/runs/{run}/winner.mp4"}

@app.get("/", response_class=HTMLResponse)
@app.get("/canvas", response_class=HTMLResponse)
def canvas(): return (ROOT / "canvas.html").read_text()

@app.get("/classic", response_class=HTMLResponse)
def index(): return (ROOT / "ui.html").read_text()

@app.get("/api/list_channels")
def api_list_channels():
    from clipcut import channel_blocks as CB
    ch, src = CB.list_channels()
    return dict(source=src, channels=[dict(name=k, platform=v.get("platform"), aspect=v.get("aspect"), ideal_s=v.get("ideal_s")) for k, v in ch.items()])

# ---- legacy v1 UI (unchanged behaviour) ----
@app.get("/v1", response_class=HTMLResponse)
def index_v1():
    return """<h2>ClipCut v1</h2><form action="/process" method="post" enctype="multipart/form-data">
    <input type="file" name="video" accept="video/*"> variants <input name="n" value="8" size="2">
    <button>Cut + score</button></form><p>Rendering takes ~1 min per minute of source.</p>"""

@app.post("/process", response_class=HTMLResponse)
def process(video: UploadFile = File(...), n: int = Form(8)):
    from clipcut.cutter import cut
    from clipcut import personas, report
    rid = uuid.uuid4().hex[:8]; d = RUNS / rid; d.mkdir()
    src = d / ("src_" + Path(video.filename).name)
    with open(src, "wb") as f: shutil.copyfileobj(video.file, f)
    m = cut(src, d, n=n)
    rank, _, curves, P = personas.score(m); report.plot_curves(rank, curves, P, d / "retention_curves.png")
    feats = {v["variant_id"]: v for v in m["variants"]}
    rows = "".join(f"<tr><td>{r['rank']}</td><td>{v}</td><td>{r['audience_score']:.3f}</td><td>{feats[v]['length_s']}s</td>"
                   f"<td>{feats[v]['jump_cuts']}</td><td>{feats[v]['caption_style']}</td><td>{feats[v]['hook_zoom']}</td>"
                   f"<td><video src='/runs/{rid}/{v}.mp4' width=140 controls></video></td></tr>" for v, r in rank.iterrows())
    return (f"<h2>Run {rid} — winner {rank.index[0]}</h2><img src='/runs/{rid}/retention_curves.png' width=900>"
            "<table border=1 cellpadding=4><tr><th>#</th><th>variant</th><th>score</th><th>len</th><th>jump</th><th>captions</th><th>zoom</th><th>preview</th></tr>"
            f"{rows}</table><p><a href='/runs/{rid}/manifest.json'>manifest.json</a></p>")

# --- retention scoring API (owner: retention agent) ---
from fastapi import Body
@app.post("/api/score")
def api_score(manifest: dict = Body(...)):
    """POST a variant manifest JSON (or {"video_path": ...}); returns per-persona swipe/watch/share predictions."""
    from clipcut.scorer import score
    return score(manifest)

@app.post("/api/persona/score")
async def api_persona_score(request: Request):
    """Explainable persona scores for the canvas. See docs/PERSONA_API.md. Does not replace POST /api/score."""
    from clipcut.persona_api import PersonaScoreError, score_variants
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "JSON object required"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "JSON object required"}, status_code=400)
    mode = body.get("mode", "auto")
    if mode is None:
        mode = "auto"
    try:
        return score_variants(run=body.get("run"), variant_ids=body.get("variant_ids"), manifests=body.get("manifests"), mode=mode)
    except PersonaScoreError as exc:
        return JSONResponse({"error": str(exc)}, status_code=exc.status)

@app.post("/api/persona/hooks")
async def api_persona_hooks(request: Request):
    """Persona-specific opening lines. See docs/PERSONA_API.md. Does not replace POST /api/score or POST /api/persona/score."""
    from clipcut.persona_api import PersonaScoreError, rewrite_hooks
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "JSON object required"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "JSON object required"}, status_code=400)
    mode = body.get("mode", "auto")
    if mode is None:
        mode = "auto"
    n = body.get("n", 3)
    if n is None:
        n = 3
    try:
        return rewrite_hooks(
            run=body.get("run"),
            variant_ids=body.get("variant_ids"),
            manifests=body.get("manifests"),
            personas=body.get("personas"),
            channels=body.get("channels"),
            n=n,
            mode=mode,
        )
    except PersonaScoreError as exc:
        return JSONResponse({"error": str(exc)}, status_code=exc.status)

if __name__ == "__main__":  # container / `python app.py`: bind all interfaces, port from $PORT (default 8000)
    import os, uvicorn
    uvicorn.run(app, host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8000")))
