"""Edit-aware feature extractor. Same code for dataset videos and generated variants.
Analyzes the first WINDOW seconds (the part that decides scroll-or-stay) + full duration from container metadata."""
import json, subprocess, numpy as np, cv2, warnings
from pathlib import Path
warnings.filterwarnings("ignore")
WINDOW = 15.0
_clip = _whisper = _face = None

def _dur(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    try: return float(r.stdout.strip())
    except ValueError: return float("nan")

def _frames(path, fps=6, w=320):
    """Decode first WINDOW s at fps, resized to width w (keeps aspect)."""
    r = subprocess.run(["ffmpeg", "-v", "error", "-t", str(WINDOW), "-i", str(path), "-vf", f"fps={fps},scale={w}:-2",
                        "-f", "rawvideo", "-pix_fmt", "bgr24", "-"], capture_output=True)
    pr = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
                        capture_output=True, text=True)
    W0, H0 = [int(x) for x in pr.stdout.strip().split(",")[:2]]
    h = int(round(H0 * w / W0 / 2) * 2)
    a = np.frombuffer(r.stdout, np.uint8)
    n = len(a) // (w * h * 3)
    return a[: n * w * h * 3].reshape(n, h, w, 3), W0, H0

def _audio(path, sr=16000):
    r = subprocess.run(["ffmpeg", "-v", "error", "-t", str(WINDOW), "-i", str(path), "-f", "s16le", "-ac", "1", "-ar", str(sr), "-"], capture_output=True)
    return np.frombuffer(r.stdout, np.int16).astype(np.float32) / 32768.0

def visual(frames, fps=6):
    global _face
    if _face is None: _face = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    gray = np.array([cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) for f in frames])
    hsv = np.array([cv2.cvtColor(f, cv2.COLOR_BGR2HSV) for f in frames])
    # shot cuts: HSV-histogram distance spikes (same idea as PySceneDetect ContentDetector, vectorized for speed)
    hists = [cv2.normalize(cv2.calcHist([h], [0, 1], None, [16, 16], [0, 180, 0, 256]), None).flatten() for h in hsv]
    d = np.array([cv2.compareHist(hists[i - 1], hists[i], cv2.HISTCMP_BHATTACHARYYA) for i in range(1, len(hists))]) if len(hists) > 1 else np.zeros(1)
    cuts = np.where(d > 0.35)[0] / fps
    # motion: Farneback optical flow magnitude on downsampled frames
    flow = []
    for i in range(1, len(gray), 2):
        f = cv2.calcOpticalFlowFarneback(cv2.resize(gray[i - 1], None, fx=.5, fy=.5), cv2.resize(gray[i], None, fx=.5, fy=.5), None, .5, 2, 9, 2, 5, 1.1, 0)
        flow.append(np.linalg.norm(f, axis=2).mean())
    flow = np.array(flow) if flow else np.zeros(1)
    # faces in first 3 s
    fa, fc = [], 0
    for f in gray[: 3 * fps : 2]:
        fs = _face.detectMultiScale(f, 1.2, 4, minSize=(20, 20))
        if len(fs): fc += 1; fa.append(max(w * h for x, y, w, h in fs) / f.size)
    # text/caption proxy: density of strong horizontal-stroke edges in lower-middle band vs elsewhere
    def edge_band(f, a, b):
        e = cv2.Canny(f[int(a * f.shape[0]): int(b * f.shape[0])], 150, 300); return e.mean() / 255
    low = np.mean([edge_band(f, .55, .85) for f in gray]); top = np.mean([edge_band(f, .1, .4) for f in gray])
    mser = cv2.MSER_create(delta=8, min_area=20, max_area=400)
    txt_blobs = np.mean([len(mser.detectRegions(f[int(.5 * f.shape[0]):])[0]) for f in gray[:: fps]])
    return dict(cuts_total_window=len(cuts), cuts_per_10s=10 * len(cuts) / max(len(gray) / fps, 1), cuts_first3s=int((cuts < 3).sum()),
                mean_shot_len=(len(gray) / fps) / (len(cuts) + 1),
                motion_mean=float(flow.mean()), motion_first3s=float(flow[: max(1, 3 * fps // 2)].mean()), motion_std=float(flow.std()),
                face_frac_first3s=fc / max(1, len(gray[: 3 * fps : 2])), face_area_first3s=float(np.mean(fa)) if fa else 0.0,
                text_edge_lower=float(low), text_edge_ratio=float(low / (top + 1e-3)), text_blobs=float(txt_blobs),
                brightness=float(hsv[..., 2].mean() / 255), contrast=float(gray.std(axis=(1, 2)).mean() / 255),
                saturation=float(hsv[..., 1].mean() / 255), hook_brightness_delta=float(hsv[:3 * fps, ..., 2].mean() / 255 - hsv[..., 2].mean() / 255))

def audio_feats(y, sr=16000, use_whisper=True):
    out = {}
    if len(y) < sr: y = np.pad(y, (0, sr - len(y)))
    fr = y[: len(y) // 400 * 400].reshape(-1, 400); rms = np.sqrt((fr ** 2).mean(1) + 1e-10); db = 20 * np.log10(rms + 1e-9)
    out.update(loudness_db=float(db.mean()), loudness_std=float(db.std()), dead_air_frac=float((db < -45).mean()))
    import librosa
    flat = librosa.feature.spectral_flatness(y=y, n_fft=1024, hop_length=512)[0]
    onset = librosa.onset.onset_strength(y=y, sr=sr)
    tempo_strength = float(np.max(librosa.autocorrelate(onset - onset.mean())[10:60]) / (np.sum((onset - onset.mean()) ** 2) + 1e-9))
    out.update(spectral_flatness=float(flat.mean()), beat_regularity=tempo_strength)
    # speech: Silero VAD (fast, language-agnostic; same for Chinese dataset videos and English variants).
    # Whisper is too slow for 1.5k videos on this shared CPU; speech "rate" = onset (syllable-ish) rate inside speech.
    from faster_whisper.vad import get_speech_timestamps, VadOptions
    ts = get_speech_timestamps(y, VadOptions(min_silence_duration_ms=250, speech_pad_ms=30))
    sp_mask = np.zeros(len(db), bool)
    for t in ts: sp_mask[t["start"] // 400: t["end"] // 400 + 1] = True
    sp_mask = sp_mask[: len(db)]
    first = ts[0]["start"] / sr if ts else WINDOW
    on = librosa.onset.onset_detect(y=y, sr=sr, units="time")
    on_sp = [t for t in on if sp_mask[min(int(t * 40), len(sp_mask) - 1)]]
    sp_sec = sp_mask.sum() / 40
    non = db[~sp_mask]
    out.update(words_per_sec=len(on_sp) / max(sp_sec, 1.0), time_to_first_word=float(first), speech_frac=float(sp_mask.mean()),
               n_speech_gaps_per_10s=10 * max(len(ts) - 1, 0) / (len(y) / sr),
               music_score=float(((non > -35).mean() if len(non) else 0) * 0.5 + min(tempo_strength, 1) * 0.5))
    return out

def clip_embed(frame_bgr, dim=16):
    """CLIP ViT-B/32 image embedding of the hook frame, projected to `dim` dims later (PCA fit on dataset)."""
    global _clip
    import torch, open_clip
    from PIL import Image
    if _clip is None:
        torch.set_num_threads(2)
        m, _, pre = open_clip.create_model_and_transforms("ViT-B-32", pretrained="laion2b_s34b_b79k"); m.eval(); _clip = (m, pre)
    m, pre = _clip
    with torch.no_grad():
        e = m.encode_image(pre(Image.fromarray(frame_bgr[..., ::-1])).unsqueeze(0))[0]
    return (e / e.norm()).numpy().tolist()

def extract(path, use_whisper=True, embed=True):
    frames, W, H = _frames(path)
    if len(frames) < 6: raise RuntimeError("too few frames")
    f = dict(duration_s=_dur(path), aspect=H / W, is_vertical=int(H > W), window_s=len(frames) / 6)
    f.update(visual(frames)); f.update(audio_feats(_audio(path), use_whisper=use_whisper))
    if embed: f["clip"] = clip_embed(frames[min(6, len(frames) - 1)])  # frame at t=1s
    return f

if __name__ == "__main__":
    import sys, time; t = time.time(); r = extract(sys.argv[1]); r["clip"] = f"<{len(r['clip'])} dims>"
    print(json.dumps(r, indent=1)); print("secs", round(time.time() - t, 1))
