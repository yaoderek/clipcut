# ClipCut — Devpost draft (DubHacks '26)

Paste into https://dubhacks-2026.devpost.com/ before **Saturday 10:30 PM PDT**. Missing this draft locks you out of Sunday judging. Final polish can wait until Sunday 10:00 AM PDT.

## Suggested tracks
- **Sponsored side track:** SideShift (Best Startup Idea)
- **Title track:** pick one (Cook Smarter / Chef's Kitchen / Community Kitchen) once you decide where it fits best
- Add every teammate as a contributor (with Discord handles) in this Saturday draft

## Project name
ClipCut

## Tagline (one line)
One long video in, ranked short-form variants out — so creators ship the cut that keeps people watching.

## Elevator (2–3 sentences)
Creators and UGC marketers guess which hook, length, and caption style will win. ClipCut turns one raw talking-head into many 9:16 variants (hook, pacing, captions, loop ending), scores them for short-form personas, and surfaces a winner with channel fit. Built for SideShift-style creator workflows: cut fast, pick with evidence, A/B with real metrics later.

## What it does
- Upload (or use the sample) a long talking-head clip
- Generates diverse vertical variants: face-aware 9:16 crop, hook-first open, jump cuts + punch-ins, word-pop or sentence captions, loop-friendly endings
- Scores each variant with a persona-aware model (plus an honest heuristic fallback) and shows ranked previews in a local web UI
- Optional A/B step: feed real post metrics (views, watch %, shares) to pick a Bayesian/Thompson winner

## How we built it
- Python cutter (ffmpeg + Whisper/SRT timing), FastAPI + web UI
- v2 engagement model trained on MicroLens video + interaction data (edit features + CLIP frame embedding); KuaiRand length prior kept for retention charts and labelled as such
- Honest limits documented: no public dataset pairs raw video with per-viewer retention, so edit→retention still needs creator A/B or YouTube Analytics

## Demo
- Local UI: `uvicorn app:app --port 8000` in `/workspace/clipcut` (already runnable)
- Sample run: `runs/v2_niosh_house/` (12 variants; heuristic winner currently `v07`)
- README with setup, metrics, and caveats: `/workspace/clipcut/README.md`

## Built with
Python, FastAPI, ffmpeg, faster-whisper / SRT, LightGBM, scikit-learn, CLIP ViT-B/32, MicroLens + KuaiRand (research datasets)

## What's next
- Learn edit→retention from SideShift creator exports / YouTube Analytics on owned channels
- Ship `gather_shorts.py` from a normal machine (box YouTube scrape is bot-blocked)
- Real A/B on posted variants instead of mock metrics

## Notes for Saturday night draft (minimum to stay eligible)
1. Create/submit the Devpost project with this name + tagline + short description
2. Add all team members + Discord handles
3. Declare SideShift (+ title track)
4. Link repo or ZIP when ready; video/demo can land in the Sunday final
