# ClipCut — one raw video → many short-form variants → simulated personas pick the winner → A/B confirms
DubHacks 2026 · SideShift "Best Startup Idea" prototype (creator/UGC marketing).

## Run locally with demo
`python cli.py demo-setup` or `make demo` fetches the public-domain samples and `runs/v2_niosh_multi` variant videos if they are missing, writes simulated metrics, and adds `runs/demo_pd_kendra` and `runs/demo_pd_calvin` (JSON clones that reuse those variant videos).
`python app.py` serves http://localhost:8000. With the env var unset, a laptop also fetches those files in the background when they are absent (`CLIPCUT_AUTO_FETCH_DEMO=1` forces it, `=0` skips it).
Metrics and `demo.json` carry `demo: true` and `simulated: true`. They are not platform results. mp4 files are downloaded, not committed.

## Setup
```bash
sudo apt-get install -y ffmpeg espeak-ng   # espeak only needed to regenerate the sample video
python3 -m venv .venv && . .venv/bin/activate
pip install fastapi uvicorn python-multipart lightgbm scikit-learn pandas numpy matplotlib faster-whisper
# real dataset (47 MB): KuaiRand-Pure, https://zenodo.org/records/10439422
mkdir -p data && curl -L -o data/pure.tgz https://zenodo.org/api/records/10439422/files/KuaiRand-Pure.tar.gz/content && tar xzf data/pure.tgz -C data
```
## Run
```bash
python cli.py train-personas                       # ~15 min on 8 CPUs -> models/personas.pkl, models/metrics.json
python cli.py cut samples/raw_test.mp4 --out runs/demo -n 8   # variants + manifest.json (~1-2 min)
python cli.py score runs/demo/manifest.json        # rankings.csv, persona_scores.csv, retention_curves.png, deploy.json
python cli.py ab runs/demo/mock_metrics.json       # Bayesian/Thompson winner from SIMULATED metrics (mock_metrics.json)
python cli.py demo samples/raw_test.mp4            # all of the above, with MOCK metrics
uvicorn app:app --port 8000                        # web UI: upload -> variants, chart, previews
```
Metrics JSON format: `{"v01": {"views": 1200, "avg_watch_pct": 54.3, "shares": 31}, ...}`.
Set `XAI_API_KEY` (or `GROK_API_KEY`) to enable the optional LLM-persona check (`clipcut/llm_persona.py`, untested stub).

## How it works
- **cutter.py**: faster-whisper (tiny, CPU) transcript → hook candidates scored by heuristic (questions, numbers, "you", power words) → variants differ in hook (first 3 s), length (15/30/45 s), jump cuts (ffmpeg silencedetect), caption style (4 burned-in ASS styles), Ken-Burns zoom on hook. 9:16 720x1280 output.
- **personas.py**: KMeans (k=5) on per-user behavior (mean watch ratio, skip<3s rate, likes, forwards, long views, activity) from **real** KuaiRand-Pure logs; per persona LightGBM regressor (watch ratio) + classifier (skip within 3 s) on content features (duration, category tag, upload type, music type, video type, hour). Variant score = persona-weighted mean area under a predicted retention curve.
- **ab.py**: Beta posteriors for watch % and share rate, Monte Carlo P(best), expected loss, Thompson traffic split.

## Honest limitations
- Data is REAL (KuaiRand-Pure: 27,280 users, 7,344 videos, 2.37M played impressions), but it has **no hook/pacing/caption features**. Only **length** (and optional category) flow through the learned model; hook/jump-cut/caption/zoom effects are a **hand-set heuristic prior** (`heuristic_multiplier`), not learned. In the demo the ranking is therefore dominated by length (shorter → higher predicted watch ratio, which the real data shows).
- Held-out accuracy (time split, last ~15% of days) is modest: watch-ratio MAE 0.11–0.34 vs 0.15–0.44 for a constant-mean baseline, R² ≈ −0.01–0.14, skip-AUC 0.53–0.62. See `models/metrics.json`.
- Retention curve is a parametric approximation (linear drop to predicted 3-s skip, then exponential decay matched to predicted watch ratio), not a per-second learned curve.
- Watch ratio from Kuaishou (China, 2022) may not transfer to TikTok/Reels/Shorts audiences.
- Demo A/B metrics (`mock_metrics.json`) are SIMULATED. Sample video is synthetic (ffmpeg test pattern + espeak TTS).
- Watch % is modeled as a Beta with views as pseudo-counts — a simplification.

---
# v2 — edit-aware (v1 above is kept unchanged)

## Data used (verified 2026-10-17)
| Dataset | Content | Engagement | Per-user | Used? |
|---|---|---|---|---|
| **MicroLens-100k** (westlake-repl) | raw MP4s (477 GB split zip), covers, titles | likes + views per video | user→video interaction pairs (no watch time) | **YES**: 1,112 random videos with ≥15 interactions; first 5 MB of each pulled via HTTP Range from the split zip (`clipcut/microlens_fetch.py`) |
| KuaiRand-Pure | no raw video (ids, tag, duration only) | per-user watch time | yes | v1 only (length-retention prior) |
| SMP-Video (HF) | video source links + popularity | video-level | no | gated (licence click-through); not downloaded |
| YouTube Shorts via yt-dlp | — | views/likes | no | `clipcut/gather_shorts.py` written; **blocked from this server** ("sign in to confirm you're not a bot"); needs to run from a normal machine |

**No dataset found combines raw video + per-user watch time / retention curves.** So v2 targets are *engagement*, not retention.

## Features (`clipcut/features.py`, same code for dataset videos and variants, first 15 s window)
Shot-cut rate and cuts in first 3 s (HSV-histogram cut detector, similar to the PySceneDetect ContentDetector, vectorized; PySceneDetect itself was not used), mean shot length, Farneback optical-flow motion (mean / first 3 s / std), Haar face presence and size in first 3 s, caption/text proxies (lower-band edge density, lower/upper ratio, MSER blobs; not OCR), brightness/contrast/saturation, hook brightness delta, loudness mean/std, dead-air fraction, spectral flatness, beat regularity, music proxy, speech fraction + time to first speech + speech-gap rate + syllable-rate proxy (Silero VAD + onsets), log duration, vertical flag, and a CLIP ViT-B/32 embedding of the t=1 s frame (PCA to 8 dims).
Whisper words/sec was dropped from the dataset features: on this shared, overloaded CPU it ran at minutes per clip, and int8 ctranslate2 returned garbage.

## Model (`clipcut/personas_v2.py`, `python train_v2.py` → `models/metrics_v2.json`)
- Personas = KMeans(k=4) on users' *content taste* (mean standardized edit features of videos they engaged with; users with ≥3 interactions).
- Persona model: LightGBM on (persona × content features) → log1p(# interactions from that persona).
- Like-rate model: LightGBM content → log(likes/views).
- Evaluation: 5-fold **GroupKFold by video** (test videos unseen). Personas are **re-clustered inside each fold on train videos only**. A first version clustered on all videos, which leaked: within-persona Spearman was 0.42, versus 0.20 once the leak was fixed.

### Held-out-video metrics (1,112 videos, 8,258 persona-user interactions, 2,386 users)
| Feature set | Persona engagement: within-persona Spearman | MAE | Like-rate Spearman | Like-rate R² |
|---|---|---|---|---|
| persona mean baseline | 0.00 | 0.464 | – | – |
| length only | 0.083 | 0.465 | 0.365 | 0.135 |
| length + edit | 0.199 | 0.451 | 0.451 | 0.209 |
| length + edit + CLIP | **0.218** | 0.447 | **0.535** | **0.320** |
| CLIP only (+length) | 0.219 | 0.444 | 0.495 | 0.279 |

Ablation: edit features beat length-only on like-rate by +0.087 Spearman (paired bootstrap 95% CI [0.043, 0.136]). Persona-engagement R² is negative for every model, the baseline included. Count levels shift between folds, so only the ranking metric is meaningful there, and that signal is weak (~0.2).
Top gain importance: persona, cuts_per_10s, speech_frac, log_duration, spectral_flatness, music_score, loudness_db, text_blobs.

## Cutter v2 audit (`clipcut/cutter_v2.py`)
| Recipe | v1 | v2 |
|---|---|---|
| 9:16 | center crop 720x1280 | face-aware crop 1080x1920 |
| hook in first 1–3 s, start on first word | hook segment, often mid-silence | opens on hook sentence's first word; hook = best/second/chronological |
| cuts every 2–4 s | silence jump cuts only | `tight_punch`: jump cuts + alternating 1.0/1.2 punch-in every ≤3 s |
| captions lower third | bottom-aligned | `sentence` (lower third, boxed) or `word_pop` (1–3 words, big yellow, lower-middle) |
| no dead air | -30 dB silences > 0.4 s | silences > 0.25 s removed (-35 dB) in tight modes |
| loop-friendly ending | none | `loop` (hard cut on last word) vs `fade` |
| hook title overlay | none | optional top title for first 3 s |
Run: `python -c "from clipcut.cutter_v2 import cut; cut('samples/talking_head_niosh.mp4','runs/v2_niosh',n=10,srt='samples/niosh_liz.en.srt',srt_offset=28.0)"` then `python score_v2.py runs/v2_niosh`.
Test video: NIOSH "Science Speaks – Liz, Epidemiologist" (public domain, Wikimedia Commons), 28–148 s. The transcript is the human SRT from Commons, with word times interpolated (Whisper was too slow on the loaded box). Word-pop timing is therefore approximate. The diversity rule yielded 6 variants (asked for 10). The 15 s target came out at 11.4 s because it snaps to sentence boundaries.

## Honest verdict
- Edit features carry **real but modest** signal for *engagement* (like-rate), clearly beyond length. Per-persona ranking signal is weak (Spearman ≈ 0.2).
- Nothing here predicts **retention**: MicroLens has no watch time. The retention chart's right panel is the v1 KuaiRand length-only prior, labelled as such.
- Domain gap: MicroLens videos are mostly horizontal, Chinese-language, often minutes long; features cover only the first 15 s (or less if 5 MB ran out). Variant scores are extrapolations.
- Next: real A/B data from SideShift creators (YouTube Analytics retention for owned channels) to learn edit→retention directly; run `gather_shorts.py` from a normal machine; more MicroLens videos (resumable: `scripts_fetch.sh` + `python -m clipcut.extract_dataset`).

---
# Scoring API (for clip-editor) — dummy-heuristic-v0
> **CAVEAT: the default scorer is a HEURISTIC, NOT TRAINED.** Every number comes from hand-set rules based on short-form best practice. None of them is a prediction learned from data. Output carries `model_version: "dummy-heuristic-v0"`, `confidence: "none - untrained heuristic"`, `label: "heuristic, not trained"`. See `docs/DATA_NEEDED.md` for the data that would make it learnable.
```python
from clipcut.scorer import score      # -> clipcut.retention.score -> score_dummy (manifest fields only, ~0.1-0.5 ms)
r = score("runs/v2_niosh12/v04.json") # path, dict, or .mp4 path
# {variant_id, overall:{p_swipe3s, watch_pct(0-100), p_share, score(0-1)},
#  personas:{fast_swiper|casual_browser|completionist|sharer_engager: {..same, channel, fit}},
#  best_persona, channels:[{channel, alt, persona, persona_score, fit, reason}] (sorted by fit),
#  reasons:[{persona, length, top:[3 strings]}], rule_inputs, model_version, confidence, label}
```
`CLIPCUT_SCORER=model` switches to the old retention-v1 model (slow, decodes video). HTTP: `POST /api/score` in app.py.
**Rules** (`clipcut/retention.py`; each rule gives a quality in [-1,1], weighted per persona):
- hook: `hook_end_s` ≤3 → +1; ≤5 → −0.3; >5 → −1.
- pace: share of shots 2–4 s long (from `cut_times_s`), ×2−1; −0.5 if any shot >8 s; no cuts → −1.
- captions present → +1, otherwise −1. `silence_removed_s` → up to +1 at 2 s; none → −0.3. `loop_end` → +1.
- length sweet spots: fast_swiper 15–30 s (OK to 40), casual 20–45 (60), completionist 30–60 (90), sharer 20–45 (60).
- Persona base rates and weights are in `PERSONAS`: fast_swiper is hook-heavy, completionist barely cares, and the sharer gets a loop bonus.
- swipe = base·exp(−0.5·(0.6·hook + 0.4·u)·hook_w/1.2), watch = sigmoid(logit(base) + 1.2u), share = base·exp(0.8u + 0.4·loop·loop_w).
- score = 0.4(1−swipe) + 0.4·watch + 0.2·min(share/0.02, 1). fit = score − persona's neutral score.
- channel per persona: fast_swiper→TikTok, casual→Reels, completionist→LinkedIn, sharer→X (Shorts is the alt).

**v0.1 changes (format-aware, more spread):**
- **Format-aware:** aspect comes from `aspect`, `recipe.aspect`, `resolution` [w,h], or ffprobe of `video_path` (metadata only). Each channel's score is 0.5 + 1.5 × (its audience persona's fit) × format_fit. format_fit is 0.6 if the aspect isn't one `CHANNEL_SPECS` accepts. Duration within ideal gives 1.0, within max 0.85, over max 0.3. Channel audiences: TikTok=fast_swiper, Reels=casual, Shorts=fast+casual, X=sharer, LinkedIn=completionist. `overall.score` is the score for the clip's own `channel` (when the manifest names one). The output also has `channels:[{channel, score, audience_fit, format_fit, reason}]` sorted by score, plus `best_channel`, `target_channel` and `audience_score` (the old channel-agnostic composite).
- **Finer rules:**
  - hook quality is continuous: 3.0 s scores 0.4 and 1.5 s scores 1.0.
  - `hook_boundary`: pause +0.3, punct +0.2, word −0.4.
  - hook line strength comes from `channels.hook_strength(hook_text)` (or `hook_strength` / `hook_score` if the manifest has one).
  - pace is penalized when shot-length CV > 0.6.
  - captions: word_pop 1.0, sentence 0.6.
  - hook title overlay +0.6 (none −0.2).
  - length is scored continuously within the sweet spot.
- `score_batch(manifests)` adds `batch_score` (0–100, min-max scaled within the batch) and `batch_rank`.
- `simulate_ab(channel, variant_manifests, n_impressions=5000, seed=0)` **SIMULATED mock A/B**. It draws views, swipes, watch % and shares around each variant's heuristic per-persona predictions for that channel (scaled by format_fit), then runs `clipcut.ab.analyze` (Beta posteriors + Thompson sampling). It returns `{channel, split, results:{v:{views, swipes_3s, swipe_rate, avg_watch_pct, shares, share_rate, heuristic_score, simulated:true}}, p_best, winner, confident, simulated:true, label}`. Results are deterministic per seed and take about 2 ms warm (the first call imports numpy, ~100 ms). `split` = next-round traffic share = P(best). ab.py ranks on watch + share only, not swipe, so its winner can differ from the heuristic score's top pick.

**v0.2:**
- `CHANNEL_SPECS` is data-driven: each entry has `audience` (personas), `platform`, aspect, max/ideal seconds, captions, safe zone and notes. Channels: TikTok, Reels, Shorts, X, LinkedIn, YouTube (16:9 long-form), IG_Feed (4:5/1:1), IG_Stories (9:16), FB_Reels (9:16), FB_Feed (1:1/4:5). Adding a channel takes one dict entry. `list_channels()` returns them for the UI.
- Each `channels[]` entry in score() output has `predicted: {views_range, watch_pct, p_swipe3s, shares_range, simulated: true}`. These are mock numbers derived from the heuristic. The views scale (`VIEWS_PER_SCORE`) is hand-set.
- `simulate_ab` now picks its winner by Thompson sampling on the same composite the heuristic uses: 0.4·stay-past-3s + 0.4·watch (of all impressions) + 0.2·share. The old `ab.py` result (watch + share only) is kept under `ab_py_watch_share_only`.

**v0.3 (hand-set, simulated):** `audience` can be weighted (`{persona: weight}`). Optional per-channel knobs: `share_weight` multiplies simulated shares and adds a share-lift term to the channel score; `views_scale` multiplies simulated views. FB_Reels = casual_browser 0.6 + completionist 0.4, ideal 20–60 s, share_weight 1.5, views_scale 0.6. IG Reels = casual_browser, ideal 15–45 s, defaults of 1.0. None of these numbers are measured.

**Channels + moment picking** (`clipcut/channels.py`): `CHANNEL_SPECS` (aspect, resolution, max/ideal seconds, captions, safe zones) for TikTok/Reels/Shorts/X/LinkedIn; `pick_moments(transcript_segments, features=None, n=5, min_len=15, max_len=45)` returns non-overlapping source spans ranked by hook strength (question, number, strong claim, "you", punchy length, optional loudness/face time series); `hook_strength(text)`; `fit_for_channel(duration_s, channel)`.

## Legacy: retention-v1 model (CLIPCUT_SCORER=model)
```python
from clipcut.scorer import score          # re-exports clipcut.retention.score
r = score("runs/v2_niosh12/v05.json")     # manifest path, dict, or raw .mp4 path
# {"variant_id", "overall": {"p_swipe3s", "watch_pct" (0-100), "p_share", "score" (0-1)},
#  "personas": {"casual_browser"|"sharer_engager"|"completionist"|"fast_swiper": {same keys}},
#  "model_version", "confidence", "edit_signal", "manifest_features"}
```
HTTP: `uvicorn app:app --port 8000`, then `curl -XPOST localhost:8000/api/score -H 'content-type: application/json' -d @runs/v2_niosh12/v05.json`.
Manifest fields used: `duration_s` (or `duration`), `video_path` (or `file`; relative paths resolve next to the manifest), `cut_times_s` (or `cuts`), `captions`, `hook_end_s`, `silence_removed_s`. Takes about 10 s per clip on CPU (features.extract plus CLIP).
Train: `python train_retention.py` → `models/retention/{retention.pkl,metrics.json}`. Code: `clipcut/retention_model.py`.

**How it works**
- Personas: KMeans(k=4) on 27,280 KuaiRand users' real behavior (watch %, swipe<3 s rate, forward, like, mean duration, activity).
- Per target, a LightGBM on (persona, log duration) at the video×persona level is trained on real KuaiRand watch logs. Swipe means play < 3 s, watch % is play/duration, and share is is_forward.
- Edit signal: the MicroLens v2 edit-aware like-rate model gives e = log(pred like-rate / mean), clipped to ±1. It is applied as `EDIT_LINK` multipliers: share ×e^(1.0e), watch ×e^(0.15e), swipe ×e^(−0.15e). **These elasticities are HAND-SET, not learned.** No public dataset pairs raw video with watch time.

**Held-out by VIDEO (5-fold GroupKFold, 7,337 videos, 23,927 video×persona cells, impression-weighted)**
| target | global mean | persona mean | popularity† | duration only | duration+persona (deployed) | +category |
|---|---|---|---|---|---|---|
| watch_pct R² / MAE | 0.00 / .152 | .25 / .130 | .40 / .115 | .36 / .118 | **.64 / .090** | .71 / .079 |
| p_swipe3s R² / MAE | 0.00 / .170 | .45 / .123 | .67 / .091 | −.05 / .173 | .42 / .127 | .54 / .111 |
| p_share R² | 0.00 | .02 | .00 | −.01 | −.00 | −.01 |
† Popularity = log(show_cnt) from KuaiRand's whole-period stats. It is partly leaky and unavailable for a new clip, so treat it as a reference point only.
Read: duration plus persona explains watch % well. Swipe needs content (category or popularity), which a new clip doesn't have; duration-only swipe is no better than the mean. **Share is not predictable from these features** (the base rate is about 0.1%). Category is not deployed because editor clips have no Kuaishou tag.
