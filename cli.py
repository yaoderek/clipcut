#!/usr/bin/env python3
"""ClipCut CLI.
  python cli.py cut VIDEO --out runs/demo -n 8
  python cli.py train-personas
  python cli.py score runs/demo/manifest.json
  python cli.py ab metrics.json
  python cli.py demo VIDEO        (cut -> score -> chart -> mock A/B)"""
import argparse, json, sys
from pathlib import Path

def cmd_cut(a):
    from clipcut.cutter import cut
    m = cut(a.video, a.out, n=a.n, seed=a.seed, model_size=a.whisper)
    print(f"wrote {len(m['variants'])} variants + {a.out}/manifest.json (transcriber={m['transcriber']})")

def cmd_score(a):
    from clipcut import personas, report, llm_persona
    m = json.load(open(a.manifest)); out = Path(a.manifest).parent
    rank, detail, curves, P = personas.score(m, tag=a.tag)
    feats = {v["variant_id"]: v for v in m["variants"]}
    for c in ["length_s", "jump_cuts", "caption_style", "hook_zoom", "hook_score"]:
        rank[c] = [feats[v][c] for v in rank.index]
    print("\nPersonas:"); [print(f"  {p}: {P[p]['name']}  weight={P[p]['weight']:.2f}") for p in P]
    print("\nAudience ranking:"); print(rank.round(3).to_string())
    rank.round(4).to_csv(out / "rankings.csv"); detail.round(4).to_csv(out / "persona_scores.csv", index=False)
    report.plot_curves(rank, curves, P, out / "retention_curves.png")
    winner = rank.index[0]
    json.dump({"winner": winner, "variant": feats[winner]}, open(out / "deploy.json", "w"), indent=2)
    print(f"\nWinner -> {winner} ({feats[winner]['file']}); wrote rankings.csv, persona_scores.csv, retention_curves.png, deploy.json")
    if llm_persona.available():
        r = llm_persona.rate(P[0]["name"], feats[winner]); print("LLM persona check:", r)
    else:
        print("LLM persona path: skipped (no XAI_API_KEY/GROK_API_KEY)")
    return rank

def cmd_ab(a):
    from clipcut.ab import analyze, thompson_allocate
    met = json.load(open(a.metrics)); r = analyze(met)
    import pandas as pd
    print(pd.DataFrame(r["table"]).round(4).to_string(index=False))
    print(f"\nWinner: {r['winner']}  (confident>=95%: {r['confident']})")
    print("Next-round Thompson traffic split (1000 views):", thompson_allocate(met))
    return r

def cmd_cut2(a):
    from clipcut.cutter_v2 import cut
    m = cut(a.video, a.out, n=a.n, seed=a.seed, model_size=a.whisper, srt=a.srt, srt_offset=a.srt_offset)
    print(f"wrote {len(m['variants'])} v2 variants (+ per-variant vNN.json) to {a.out}")
    if not a.no_score:
        import subprocess, sys; subprocess.run([sys.executable, str(Path(__file__).parent / "rank_v2.py"), str(a.out)], check=True)

def cmd_train(a):
    from clipcut import personas; personas.train(k=a.k)

def cmd_demo(a):
    out = Path(a.out)
    cmd_cut(argparse.Namespace(video=a.video, out=out, n=a.n, seed=0, whisper=a.whisper))
    rank = cmd_score(argparse.Namespace(manifest=out / "manifest.json", tag=None))
    # MOCK metrics, clearly labeled: simulate a small live test seeded by predicted scores
    import numpy as np; rng = np.random.default_rng(1); met = {}
    for v, s in rank["audience_score"].items():
        views = int(rng.integers(800, 1500)); met[v] = {"views": views, "avg_watch_pct": float(np.clip(rng.normal(s, 0.04), 0, 1)) * 100,
                                                        "shares": int(rng.binomial(views, 0.01 + 0.03 * s))}
    payload = {"_meta": {"simulated": True, "generator": "cli.py demo", "note": "Simulated from predicted scores. Not platform data."}, **met}
    json.dump(payload, open(out / "mock_metrics.json", "w"), indent=2)
    print("\n[A/B] on MOCK metrics (simulated, not real platform data):")
    cmd_ab(argparse.Namespace(metrics=out / "mock_metrics.json"))

p = argparse.ArgumentParser(); s = p.add_subparsers(dest="cmd", required=True)
c = s.add_parser("cut"); c.add_argument("video"); c.add_argument("--out", default="runs/latest"); c.add_argument("-n", type=int, default=8)
c.add_argument("--seed", type=int, default=0); c.add_argument("--whisper", default="tiny"); c.set_defaults(f=cmd_cut)
c = s.add_parser("score"); c.add_argument("manifest"); c.add_argument("--tag", default=None); c.set_defaults(f=cmd_score)
c = s.add_parser("ab"); c.add_argument("metrics"); c.set_defaults(f=cmd_ab)
c = s.add_parser("train-personas"); c.add_argument("-k", type=int, default=5); c.set_defaults(f=cmd_train)
c = s.add_parser("demo"); c.add_argument("video"); c.add_argument("--out", default="runs/demo"); c.add_argument("-n", type=int, default=8)
c.add_argument("--whisper", default="tiny"); c.set_defaults(f=cmd_demo)

def cmd_demo_setup(_a):
    from clipcut.demo_setup import main
    raise SystemExit(main())

c = s.add_parser("demo-setup", help="download missing demo videos, render extra clips, write simulated metrics")
c.set_defaults(f=cmd_demo_setup)
c = s.add_parser("cut2", help="v2 recipe cutter -> 10+ variants -> scorer -> results.json"); c.add_argument("video"); c.add_argument("--out", default="runs/v2_latest")
c.add_argument("-n", type=int, default=12); c.add_argument("--seed", type=int, default=0); c.add_argument("--whisper", default="base")
c.add_argument("--srt", default=None); c.add_argument("--srt-offset", type=float, default=0.0); c.add_argument("--no-score", action="store_true"); c.set_defaults(f=cmd_cut2)
a = p.parse_args(); a.f(a)
